"""查询编译、优化和执行层的目录边界回归。"""

from yoursql.execution import ValuesExecutor
from yoursql.planner.logical import LogicalPlanNode
from yoursql.planner.optimizer import Optimizer
from yoursql.planner.physical import PhysicalPlanNode
from yoursql.sql.compiler import Compiler


def test_compiler_emits_logical_plan_and_optimizer_emits_physical_plan() -> None:
    compilation = Compiler().compile("SELECT 1;")

    assert isinstance(compilation.plan, LogicalPlanNode)
    optimized = Optimizer().optimize(compilation.plan)
    assert isinstance(optimized, PhysicalPlanNode)


def test_execution_package_exposes_volcano_operator() -> None:
    assert list(ValuesExecutor([(1,), (2,)])) == [(1,), (2,)]
