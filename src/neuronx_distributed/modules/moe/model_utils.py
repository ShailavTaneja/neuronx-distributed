from typing import Dict, Callable

import torch
import torch.nn.functional as F

ACT2FN: Dict[str, Callable] = {
    "gelu": F.gelu,
    "leaky_relu": F.leaky_relu,
    "relu": F.relu,
    "sigmoid": torch.sigmoid,
    "silu": F.silu,
    "tanh": torch.tanh,
}
