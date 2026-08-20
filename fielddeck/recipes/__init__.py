"""Repeatable test recipes: YAML in, an execution plan out, evidence behind.

A recipe is how a bring-up procedure stops living in someone's head.  The
engine is deliberately split into four pieces with one rule between them:

``schema``
    What a recipe file may say.  Strict — unknown keys are refused, because a
    silently ignored ``current_limit`` is a DUT energised at the wrong setpoint.

``compiler``
    What the file *would do*, decided before anything does it: devices,
    actions, the permission each call resolves to, and every limit violation
    that can be seen statically.  A recipe that would exceed the deployment's
    limits fails here, not at step seven with the output on.

``assertions``
    A tiny, ``eval``-free expression language for ``assert`` steps, because a
    recipe is the part of FieldDeck most likely to have been written by a
    stranger.

``runner``
    Execution, as an ordinary client with no authority of its own.  A recipe
    cannot arm anything; an operator must have armed what it needs, and the
    runner says so up front rather than failing partway through.

The whole engine holds one promise: **nothing physical happens until the entire
file has been checked**, and the ``finally`` steps run whatever happens next.
"""

from __future__ import annotations

from fielddeck.recipes.assertions import (
    AssertionOutcome,
    CompiledExpression,
    compile_expression,
    evaluate_assertion,
)
from fielddeck.recipes.compiler import (
    ExecutionPlan,
    PlannedStep,
    PlanProblem,
    ProblemSeverity,
    compile_recipe,
)
from fielddeck.recipes.runner import RecipeRun, RecipeRunner, RecipeState, StepOutcome, StepRecord
from fielddeck.recipes.schema import (
    Recipe,
    RecipePhase,
    RecipeSource,
    StepKind,
    load_recipe_file,
    load_recipe_text,
    recipe_roots,
    resolve_recipe_reference,
)

__all__ = [
    "AssertionOutcome",
    "CompiledExpression",
    "ExecutionPlan",
    "PlanProblem",
    "PlannedStep",
    "ProblemSeverity",
    "Recipe",
    "RecipePhase",
    "RecipeRun",
    "RecipeRunner",
    "RecipeSource",
    "RecipeState",
    "StepKind",
    "StepOutcome",
    "StepRecord",
    "compile_expression",
    "compile_recipe",
    "evaluate_assertion",
    "load_recipe_file",
    "load_recipe_text",
    "recipe_roots",
    "resolve_recipe_reference",
]
