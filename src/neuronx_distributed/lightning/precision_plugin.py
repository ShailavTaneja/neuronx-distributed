from typing import Any, Callable, TYPE_CHECKING

from lightning.fabric.accelerators.xla import _XLA_AVAILABLE
from lightning.fabric.utilities.types import Optimizable
from lightning.pytorch.plugins.precision import XLAPrecision

if TYPE_CHECKING:
    import lightning.pytorch as pl


class NeuronXLAPrecisionPlugin(XLAPrecision):
    def __init__(self, mixed_precision_enabled: bool = False) -> None:
        if not _XLA_AVAILABLE:
            raise ModuleNotFoundError(str(_XLA_AVAILABLE))

        self.mixed_precision_enabled = mixed_precision_enabled

    def optimizer_step(  # type: ignore[override]
        self,
        optimizer: Optimizable,
        model: "pl.LightningModule",
        closure: Callable[[], Any],
        **kwargs: Any,
    ) -> Any:
        # TODO: currently using manual optimization, need further modification here for auto optimization
        optimizer.step()
