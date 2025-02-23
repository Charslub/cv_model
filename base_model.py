import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image
from torch import Tensor
from torch_geometric.nn import GCNConv
from torchvision import models, transforms
import pandas as pd


# ---------------------- CNN特征提取模块 ----------------------
class MultiScaleFeatureExtractor(nn.Module):
    """多尺度特征提取器"""

    def __init__(self,
                 cnn_type: str = 'resnet50',
                 pretrained: bool = True,
                 output_dims: int = 2048,
                 layers_to_extract: list = ['layer2', 'layer3', 'layer4']  # 修改默认值
                 ):
        super().__init__()
        self.cnn_type = cnn_type
        self.output_dims = output_dims
        self.layers_to_extract = layers_to_extract

        # 加载预训练主干网络并拆分层
        backbone = models.__dict__[cnn_type](pretrained=pretrained)

        # 提取所有必要层（包含初始卷积层）
        self.initial_layers = nn.Sequential(
            backbone.conv1,  # 输入通道3 → 64
            backbone.bn1,
            backbone.relu,
            backbone.maxpool  # 输出通道64
        )
        self.feature_layers = nn.ModuleDict({
            'layer1': backbone.layer1,  # 输入64 → 输出256
            'layer2': backbone.layer2,  # 输入256 → 输出512
            'layer3': backbone.layer3,  # 输入512 → 输出1024
            'layer4': backbone.layer4  # 输入1024 → 输出2048
        })

        # 多尺度特征融合
        self.fuse = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Flatten(),
            nn.Linear(self._get_total_channels(cnn_type, layers_to_extract), output_dims),
            nn.BatchNorm1d(output_dims, affine=True),  # 添加affine参数
            nn.ReLU()
        )

    def _get_total_channels(self, cnn_type: str, layers: list) -> int:
        """计算多尺度特征的总通道数"""
        channel_map = {
            'resnet50': {'layer1': 256, 'layer2': 512, 'layer3': 1024, 'layer4': 2048},
            'resnet101': {'layer1': 256, 'layer2': 512, 'layer3': 1024, 'layer4': 2048}
        }
        return sum([channel_map[cnn_type][layer] for layer in layers])

    def forward(self, x: Tensor) -> Tensor:
        # 先经过初始卷积层
        x = self.initial_layers(x)  # [B, 64, H/4, W/4]

        # 提取指定层的多尺度特征
        features = []
        current_feat = x  # 保存当前特征，避免重复计算
        for name, layer in self.feature_layers.items():
            current_feat = layer(current_feat)  # 连续处理特征
            if name in self.layers_to_extract:
                features.append(current_feat)

        # 多尺度特征融合
        if len(features) > 1:
            target_size = features[-1].shape[2:]
            resized_features = [
                                   F.interpolate(feat, size=target_size, mode='bilinear', align_corners=False)
                                   for feat in features[:-1]
                               ] + [features[-1]]
            fused = torch.cat(resized_features, dim=1)
        else:
            fused = features[0]

        # 降维到固定维度
        return F.normalize(self.fuse(fused), p=2, dim=1)


# ---------------------- 加权GAT模块 ----------------------
class WeightedMultiHeadGAT(nn.Module):
    """支持加权邻接矩阵的魔改多头GAT（可修改部分已标注）"""

    def __init__(self,
                 in_features: int,
                 out_features: int,
                 heads: int = 12,
                 edge_dim: int = 1,
                 dropout: float = 0.3,
                 alpha: float = 0.2,
                 residual: bool = True,
                 use_edge_weights: bool = True):
        super().__init__()
        self.heads = heads
        self.alpha = alpha
        self.dropout = dropout
        self.residual = residual  # 新增
        self.use_edge_weights = use_edge_weights

        # 添加维度校验
        assert out_features % heads == 0, f"out_features({out_features}) must be divisible by heads({heads})"
        self.out_features_per_head = out_features // heads  # 修正为每个头的维度

        # 修正线性层维度
        self.W = nn.Linear(in_features, heads * self.out_features_per_head, bias=False)

        # 修正多头融合层
        self.head_fusion = nn.Sequential(
            nn.Linear(heads * self.out_features_per_head, out_features),  # 输入维度修正
            nn.GELU(),
            nn.Dropout(dropout)
        )

        # 注意力系数计算（含边权重）
        if use_edge_weights:
            self.edge_encoder = nn.Linear(edge_dim, self.heads)  # 边权重编码
        self.attn_src = nn.Parameter(torch.Tensor(1, self.heads, self.out_features_per_head))
        self.attn_dst = nn.Parameter(torch.Tensor(1, self.heads, self.out_features_per_head))

        # 残差连接
        if residual:
            self.res_fc = nn.Linear(in_features, heads * self.out_features_per_head)

        # 初始化参数
        nn.init.xavier_uniform_(self.W.weight)
        nn.init.xavier_uniform_(self.attn_src)
        nn.init.xavier_uniform_(self.attn_dst)
        if use_edge_weights:
            nn.init.xavier_uniform_(self.edge_encoder.weight)

    def forward(self, x: Tensor, adj_matrix: Tensor) -> Tensor:
        num_nodes = x.size(0)

        # 1. 线性变换 + 分头
        h = self.W(x)  # [num_nodes, heads * out_features_per_head]
        h = h.view(num_nodes, self.heads, self.out_features_per_head)  # [N, H, D_h]

        # 2. 计算注意力分数（含边权重）
        attn_src = torch.sum(h * self.attn_src, dim=-1)  # [N, H]
        attn_dst = torch.sum(h * self.attn_dst, dim=-1)  # [N, H]
        attn = attn_src.unsqueeze(1) + attn_dst.unsqueeze(0)  # [N, N, H]

        # 边权重编码（如果启用）
        if self.use_edge_weights:
            edge_weights = self.edge_encoder(adj_matrix.unsqueeze(-1))  # [N, N, H]
            attn = attn + edge_weights

        # 3. 掩码处理（仅保留邻接矩阵中的边）
        mask = (adj_matrix > 0).unsqueeze(-1)  # [N, N, 1]
        attn = attn.masked_fill(~mask, float('-inf'))

        # 4. 注意力归一化
        attn = F.leaky_relu(attn, self.alpha)  # [N, N, H]
        attn = F.softmax(attn, dim=1)  # 按目标节点归一化
        attn = F.dropout(attn, p=self.dropout, training=self.training)

        # 5. 特征聚合（加权求和）
        h_out = torch.einsum('ijh,jhd->ihd', attn, h)  # [N, H, D_h]
        h_out = h_out.reshape(num_nodes, self.heads * self.out_features_per_head)  # [N, H*D_h]

        # 6. 残差连接
        if self.res_fc is not None:
            res = self.res_fc(x)
            h_out = h_out + res

        # 修改后的多头融合方式
        h_out = self.head_fusion(h_out)  # 新增多头交互层

        return h_out


class StackedGAT(nn.Module):
    """堆叠多层魔改GAT（可修改部分已标注）"""

    def __init__(self,
                 in_features: int,
                 hidden_dims: list = [1024, 512, 256],  # 确保每层维度是heads的整数倍
                 heads: int = 12,
                 dropout: float = 0.3,
                 edge_dim: int = 1,
                 residual: bool = True):
        super().__init__()
        self.layers = nn.ModuleList()
        dims = [in_features] + hidden_dims

        # 添加维度校验
        for i, h_dim in enumerate(hidden_dims):
            assert h_dim % heads == 0, f"hidden_dim[{i}]({h_dim}) must be divisible by heads({heads})"

        # 构建GAT层
        for in_dim, out_dim in zip(dims[:-1], dims[1:]):
            self.layers.append(
                WeightedMultiHeadGAT(
                    in_features=in_dim,
                    out_features=out_dim,
                    heads=heads,
                    dropout=dropout,
                    edge_dim=edge_dim,
                    residual=residual
                )
            )

    def forward(self, x: Tensor, adj_matrix: Tensor) -> Tensor:
        for layer in self.layers:
            x = layer(x, adj_matrix)
        return x


# ---------------------- GCN分类模块 ----------------------
class GCNClassifier(nn.Module):
    """GCN分类器（可修改部分已标注）"""

    def __init__(self,
                 in_features: int,
                 num_classes: int,  # 可修改：类别数
                 hidden_dims: list = [128]  # 可修改：隐藏层维度
                 ):
        super().__init__()
        self.layers = nn.ModuleList()
        dims = [in_features] + hidden_dims + [num_classes]
        for i in range(len(dims) - 1):
            self.layers.append(
                GCNConv(dims[i], dims[i + 1])
            )
            if i != len(dims) - 2:
                self.layers.append(nn.ReLU())
                self.layers.append(nn.BatchNorm1d(dims[i + 1]))

    def forward(self, x: Tensor, edge_index: Tensor) -> Tensor:
        for layer in self.layers:
            if isinstance(layer, GCNConv):
                x = layer(x, edge_index)
            else:
                x = layer(x)
        return torch.sigmoid(x)  # 多标签分类用Sigmoid


# ---------------------- 端到端模型 ----------------------
class FullModel(nn.Module):
    """端到端模型（修正输入处理）"""

    def __init__(self,
                 cnn_feat_dim: int = 2048,
                 gat_dims: list = [1020, 504, 252],  # 调整为12的倍数
                 num_classes: int = None,
                 gat_heads: int = 12,
                 gat_dropout: float = 0.3):
        super().__init__()
        if num_classes is None:
            train_df = pd.read_csv("data/train_labels.csv")
            num_classes = len(train_df.columns) - 1

        # 1. 修改CNN特征提取器
        self.cnn = MultiScaleFeatureExtractor(
            cnn_type='resnet50',  # 显式指定模型类型
            pretrained=True,  # 确保使用预训练权重
            output_dims=cnn_feat_dim,
            layers_to_extract=['layer3', 'layer4']  # 减少使用的层数
        )

        # 2. 简化GAT结构
        self.gat = StackedGAT(
            in_features=cnn_feat_dim,
            hidden_dims=gat_dims,  # 使用修正后的维度
            heads=gat_heads,
            dropout=gat_dropout
        )

        # 3. 调整GCN分类器
        self.gcn = GCNClassifier(
            in_features=gat_dims[-1],
            num_classes=num_classes,
            hidden_dims=[256]  # 简化隐藏层
        )

        # 4. 添加Dropout层
        self.dropout = nn.Dropout(0.3)

        # 增加GAT后归一化
        self.gat_norm = nn.LayerNorm(gat_dims[-1])

    def forward(self, x_img: Tensor) -> Tensor:
        # 1. CNN特征提取
        features = self.cnn(x_img)
        features = self.dropout(features)

        # 2. 构建加权邻接矩阵（使用余弦相似度）
        sim_matrix = F.cosine_similarity(
            features.unsqueeze(1),
            features.unsqueeze(0),
            dim=2
        )
        adj_matrix = (sim_matrix + 1) / 2

        # 3. GAT特征增强
        gat_out = self.gat(features, adj_matrix)
        gat_out = self.gat_norm(gat_out)  # 新增层归一化
        gat_out = F.leaky_relu(gat_out, 0.2)
        gat_out = self.dropout(gat_out)

        # 4. GCN分类
        edge_index = adj_matrix.nonzero().t()
        logits = self.gcn(gat_out, edge_index)

        return logits


class ImageGraphPipeline(nn.Module):
    """端到端图像处理管道"""

    def __init__(self,
                 model: nn.Module,
                 img_size: int = 224,
                 mean: list = [0.485, 0.456, 0.406],
                 std: list = [0.229, 0.224, 0.225]):
        super().__init__()
        self.model = model
        self.preprocess = transforms.Compose([
            transforms.Resize((img_size, img_size)),
            transforms.ToTensor(),
            transforms.Normalize(mean=mean, std=std)
        ])

    def forward(self, image_path: str) -> Tensor:
        """处理单张图像时自动切换评估模式"""
        # 确保使用评估模式
        self.model.eval()

        with torch.no_grad():
            img = Image.open(image_path).convert('RGB')
            img_tensor = self.preprocess(img).unsqueeze(0)  # [1, C, H, W]

            # 临时禁用BatchNorm的验证
            with torch.no_grad():
                output = self.model(img_tensor)

        return output[0]


class FocalLoss(nn.Module):
    def __init__(self, gamma=2.0, alpha=0.25):
        super().__init__()
        self.gamma = gamma
        self.alpha = alpha

    def forward(self, inputs, targets):
        bce_loss = F.binary_cross_entropy_with_logits(
            inputs, targets, reduction='none')
        pt = torch.exp(-bce_loss)
        focal_loss = self.alpha * (1 - pt) ** self.gamma * bce_loss
        return focal_loss.mean()


# ---------------------- 测试用例 ----------------------
if __name__ == "__main__":
    # 初始化完整管道
    pipeline = ImageGraphPipeline(FullModel())

    # 单张图像处理示例
    output = pipeline("images/image02.jpg")
    print("单张图像预测结果:", output.shape)  # [20]

    # 批量处理示例
    batch_images = [torch.randn(3, 224, 224) for _ in range(4)]
    batch_tensor = torch.stack(batch_images)  # [4, 3, 224, 224]
    print("批量预测维度:", FullModel()(batch_tensor).shape)  # [4, 20]

    # 测试GAT层初始化
    layer = WeightedMultiHeadGAT(
        in_features=2048,
        out_features=1020,
        heads=12,
        alpha=0.3  # 显式设置alpha值
    )
    print(layer.alpha)  # 应输出0.3

    # 测试残差连接配置
    layer_with_res = WeightedMultiHeadGAT(2048, 1020, residual=True)
    layer_without_res = WeightedMultiHeadGAT(2048, 1020, residual=False)

    print("带残差连接:", hasattr(layer_with_res.res_fc, 'weight'))  # 应输出True
    print("无残差连接:", layer_without_res.res_fc is None)  # 应输出True
