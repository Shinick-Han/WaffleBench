"""Small residual adapter/head; pretrained U-Net is always frozen separately."""
import torch
from torch import nn


class SmallHead(nn.Module):
    def __init__(self, base_head, adapter=False):
        super().__init__()
        import copy
        self.head = copy.deepcopy(base_head)
        for parameter in self.head.parameters():
            parameter.requires_grad_(True)
        self.adapter = nn.Sequential(nn.Conv2d(8, 8, 3, padding=1), nn.ReLU(),
                                     nn.Conv2d(8, 8, 1)) if adapter else None
        if adapter:
            nn.init.zeros_(self.adapter[-1].weight)
            nn.init.zeros_(self.adapter[-1].bias)

    def adapted(self, x):
        return x if self.adapter is None else x + self.adapter(x)

    def forward(self, x):
        return self.head(self.adapted(x))


def masked_loss(logits, truth, valid):
    """Each tile contributes equally; padded pixels have exactly zero loss weight."""
    bce = torch.nn.functional.binary_cross_entropy_with_logits(logits, truth, reduction='none')
    denom = valid.sum((1, 2, 3)).clamp_min(1)
    bce = (bce * valid).sum((1, 2, 3)) / denom
    p = torch.sigmoid(logits) * valid
    dice = 1 - (2 * (p * truth).sum((1, 2, 3)) + 1) / (
        p.sum((1, 2, 3)) + (truth * valid).sum((1, 2, 3)) + 1)
    return (bce + dice).mean()
