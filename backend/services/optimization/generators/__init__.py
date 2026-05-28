"""Optimization variant generators (Layer 2).

Stage A ships only SettingsSweepGenerator. Stage B adds
ExpressionRewriteGenerator + CompositeGenerator; Stage C adds
GeneticOptimizerGenerator. All share the VariantGenerator protocol.
"""

from backend.services.optimization.generators.settings_sweep import (
    SettingsSweepGenerator,
)

__all__ = ["SettingsSweepGenerator"]
