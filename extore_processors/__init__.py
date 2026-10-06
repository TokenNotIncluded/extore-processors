"""Pinned, reviewed processors. All customer inputs and outputs live in code."""

from .catalog import (
    Processor,
    ProcessorContext,
    ProcessorError,
    ShopContext,
    catalog,
    get_processor,
    get_spec,
    run,
    validate_configuration,
    validate_parameters,
)

__version__ = "0.2.0"

__all__ = [
    "Processor",
    "ProcessorContext",
    "ProcessorError",
    "ShopContext",
    "catalog",
    "get_processor",
    "get_spec",
    "run",
    "validate_configuration",
    "validate_parameters",
]
