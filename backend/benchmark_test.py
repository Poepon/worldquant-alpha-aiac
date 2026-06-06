"""
Alpha Mining System - Benchmark & Effectiveness Test

这个脚本用于测试和对比系统改进效果，提供直观的指标展示。

功能:
1. 知识库状态检查和初始化
2. 各组件功能验证
3. 模拟挖掘效果对比
4. 生成可视化报告

使用方法:
    python backend/benchmark_test.py --full    # 完整测试
    python backend/benchmark_test.py --quick   # 快速检查
    python backend/benchmark_test.py --seed    # 仅初始化知识库
"""

import asyncio
import sys
import os
import json
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

# Configure logger for cleaner output
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")

# Fix Windows console encoding
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


# =============================================================================
# Benchmark Results
# =============================================================================

@dataclass
class ComponentStatus:
    """单个组件的状态"""
    name: str
    status: str  # "OK", "WARNING", "ERROR"
    message: str
    details: Dict = field(default_factory=dict)


@dataclass
class BenchmarkReport:
    """完整的基准测试报告"""
    timestamp: datetime = field(default_factory=datetime.now)
    
    # 组件状态
    components: List[ComponentStatus] = field(default_factory=list)
    
    # 知识库指标
    knowledge_base: Dict = field(default_factory=dict)
    
    # 模拟测试结果
    simulation_results: Dict = field(default_factory=dict)
    
    # 改进评估
    improvement_assessment: Dict = field(default_factory=dict)
    
    def add_component(self, name: str, status: str, message: str, details: Dict = None):
        self.components.append(ComponentStatus(
            name=name,
            status=status,
            message=message,
            details=details or {}
        ))
    
    def print_report(self):
        """打印格式化的报告"""
        print("\n" + "=" * 70)
        print("[TEST] Alpha Mining System - Effect Test Report")
        print("=" * 70)
        print(f"Test Time: {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 组件状态
        print("\n[COMPONENTS] Status Check:")
        print("-" * 50)
        for comp in self.components:
            icon = "[OK]" if comp.status == "OK" else "[WARN]" if comp.status == "WARNING" else "[ERR]"
            print(f"  {icon} {comp.name}: {comp.message}")
            if comp.details:
                for k, v in comp.details.items():
                    print(f"      - {k}: {v}")
        
        # 知识库状态
        if self.knowledge_base:
            print("\n[KB] Knowledge Base Status:")
            print("-" * 50)
            for k, v in self.knowledge_base.items():
                print(f"  * {k}: {v}")
        
        # 模拟测试结果
        if self.simulation_results:
            print("\n[SIM] Simulation Results:")
            print("-" * 50)
            for k, v in self.simulation_results.items():
                if isinstance(v, float):
                    print(f"  * {k}: {v:.4f}")
                else:
                    print(f"  * {k}: {v}")
        
        # 改进评估
        if self.improvement_assessment:
            print("\n[EVAL] Improvement Assessment:")
            print("-" * 50)
            for k, v in self.improvement_assessment.items():
                print(f"  * {k}: {v}")
        
        print("\n" + "=" * 70)


# =============================================================================
# Component Tests
# =============================================================================

def test_knowledge_seed():
    """测试知识库种子模块"""
    try:
        from backend.agents.knowledge_seed import (
            ALPHA_101_PATTERNS,
            CATEGORY_PATTERNS,
            COMPREHENSIVE_PITFALLS,
            REGION_OPTIMIZATIONS,
            get_patterns_for_dataset_category,
            get_region_config
        )
        
        stats = {
            "101_patterns": len(ALPHA_101_PATTERNS),
            "category_count": len(CATEGORY_PATTERNS),
            "pitfalls": len(COMPREHENSIVE_PITFALLS),
            "regions": len(REGION_OPTIMIZATIONS),
        }
        
        # 验证模式质量
        pv_patterns = get_patterns_for_dataset_category("pv6")
        analyst_patterns = get_patterns_for_dataset_category("analyst15")
        
        if stats["101_patterns"] >= 10 and len(pv_patterns) > 0:
            return "OK", f"共 {stats['101_patterns']} 个核心模式", stats
        else:
            return "WARNING", "模式数量不足", stats
            
    except Exception as e:
        return "ERROR", str(e), {}


def test_rag_service():
    """测试RAG服务"""
    try:
        from backend.agents.services.rag_service import (
            infer_dataset_category,
            RAGResult
        )
        
        # 测试类别推断
        test_cases = [
            ("pv6", "pv"),
            ("analyst15", "analyst"),
            ("fundamental_data", "fundamental"),
            ("news_feed", "news"),
        ]
        
        passed = 0
        for dataset_id, expected in test_cases:
            if infer_dataset_category(dataset_id) == expected:
                passed += 1
        
        accuracy = passed / len(test_cases)
        
        if accuracy == 1.0:
            return "OK", "类别推断100%准确", {"accuracy": f"{accuracy:.0%}"}
        elif accuracy >= 0.75:
            return "WARNING", f"类别推断{accuracy:.0%}准确", {"accuracy": f"{accuracy:.0%}"}
        else:
            return "ERROR", f"类别推断准确率低: {accuracy:.0%}", {}
            
    except Exception as e:
        return "ERROR", str(e), {}


def test_adaptive_thresholds():
    """测试自适应阈值"""
    try:
        from backend.alpha_scoring import get_thresholds, evaluate_alpha_comprehensive
        
        # 获取不同区域的阈值
        usa = get_thresholds("USA")
        kor = get_thresholds("KOR")
        news_usa = get_thresholds("USA", dataset_category="news")
        
        thresholds = {
            "USA_sharpe_min": usa.sharpe_min,
            "KOR_sharpe_min": kor.sharpe_min,
            "NEWS_USA_sharpe_min": round(news_usa.sharpe_min, 3),
        }
        
        # 验证阈值逻辑
        if kor.sharpe_min < usa.sharpe_min and news_usa.sharpe_min < usa.sharpe_min:
            return "OK", "区域和类别自适应阈值正常", thresholds
        else:
            return "WARNING", "阈值调整逻辑可能有问题", thresholds
            
    except Exception as e:
        return "ERROR", str(e), {}


def test_diversity_tracker():
    """测试多样性追踪器"""
    try:
        from backend.diversity_tracker import DiversityTracker, ExplorationRecord
        
        tracker = DiversityTracker()
        
        # 记录一些尝试
        for i in range(5):
            record = ExplorationRecord(
                dataset_id=f"dataset{i % 2}",
                region="USA",
                universe="TOP3000",
                operators_used=["ts_rank", "ts_delta"],
                was_successful=i == 0
            )
            tracker.record_attempt(record)
        
        # 评估新组合的多样性
        score = tracker.evaluate_diversity(
            dataset_id="new_dataset",
            fields=["new_field"],
            operators=["new_operator"]
        )
        
        stats = {
            "attempts_tracked": len(tracker.attempts),
            "new_combo_diversity": f"{score.overall_score:.2f}",
        }
        
        if score.overall_score > 0.5:
            return "OK", "多样性追踪正常工作", stats
        else:
            return "WARNING", "多样性评分可能偏低", stats
            
    except Exception as e:
        return "ERROR", str(e), {}


def test_feedback_agent():
    """测试反馈代理"""
    try:
        from backend.agents.feedback_agent import classify_failure, FAILURE_CATEGORIES
        
        # 测试失败分类
        test_cases = [
            ("quality_fail", "sharpe below threshold", {"sharpe": 0.3}, "LOW_SHARPE"),
            ("quality_fail", "high turnover", {"turnover": 0.9}, "HIGH_TURNOVER"),
            ("syntax", "invalid syntax", {}, "SYNTAX_ERROR"),
        ]
        
        passed = 0
        for err_type, err_msg, metrics, expected in test_cases:
            analysis = classify_failure(err_type, err_msg, metrics)
            if analysis.category == expected:
                passed += 1
        
        accuracy = passed / len(test_cases)
        
        stats = {
            "classification_accuracy": f"{accuracy:.0%}",
            "categories_defined": len(FAILURE_CATEGORIES),
        }
        
        if accuracy >= 0.8:
            return "OK", "失败分类准确率高", stats
        else:
            return "WARNING", f"分类准确率: {accuracy:.0%}", stats
            
    except Exception as e:
        return "ERROR", str(e), {}


def test_metrics_tracker():
    """测试指标追踪器"""
    try:
        from backend.metrics_tracker import MetricsTracker, RoundMetrics
        
        tracker = MetricsTracker(task_id=999)
        session = tracker.start_session("test_session")
        
        # 模拟一轮挖掘
        round_metrics = tracker.create_round_metrics(round_id=1, dataset_id="test", region="USA")
        
        for i in range(10):
            tracker.track_alpha_result(
                round_metrics=round_metrics,
                expression=f"test_{i}",
                passed=i % 3 == 0,
                sharpe=0.5 + i * 0.1,
                fitness=0.8,
                turnover=0.5
            )
        
        tracker.calculate_diversity_score(round_metrics)
        tracker.complete_round(round_metrics)
        
        stats = {
            "pass_rate": f"{round_metrics.pass_rate:.1%}",
            "avg_sharpe": f"{round_metrics.avg_sharpe:.2f}",
            "diversity_score": f"{round_metrics.diversity_score:.2f}",
        }
        
        return "OK", f"追踪正常 (pass_rate={stats['pass_rate']})", stats
        
    except Exception as e:
        return "ERROR", str(e), {}


# =============================================================================
# Simulation Tests
# =============================================================================

def simulate_alpha_evaluation():
    """模拟Alpha评估流程，对比新旧逻辑"""
    from backend.alpha_scoring import evaluate_alpha_comprehensive, get_thresholds
    
    # 模拟不同质量的Alpha
    test_alphas = [
        {"name": "Excellent", "is": {"sharpe": 1.8, "fitness": 1.3, "turnover": 0.4}, "os": {"sharpe": 1.5}},
        {"name": "Medium", "is": {"sharpe": 1.0, "fitness": 0.8, "turnover": 0.5}, "os": {"sharpe": 0.7}},
        {"name": "Borderline", "is": {"sharpe": 0.8, "fitness": 0.6, "turnover": 0.6}, "os": {"sharpe": 0.5}},
        {"name": "Poor", "is": {"sharpe": 0.3, "fitness": 0.3, "turnover": 0.8}, "os": {"sharpe": 0.1}},
        {"name": "HighTurnover", "is": {"sharpe": 1.2, "fitness": 1.0, "turnover": 0.9}, "os": {"sharpe": 0.8}},
    ]
    
    results = []
    
    print("\n[SIM] Alpha Evaluation Test:")
    print("-" * 75)
    print(f"{'Name':<15} {'Sharpe':<8} {'Result':<12} {'Score':<8} {'Issues'}")
    print("-" * 75)
    
    for alpha in test_alphas:
        eval_result = evaluate_alpha_comprehensive(
            sim_result=alpha,
            region="USA"
        )
        
        status = "PASS" if eval_result.passed else "OPTIMIZE" if eval_result.quality_status == "OPTIMIZE" else "REJECT"
        issues = ", ".join(eval_result.failed_tests[:2]) if eval_result.failed_tests else "None"
        
        print(f"{alpha['name']:<15} {alpha['is']['sharpe']:<8.2f} {status:<12} {eval_result.composite_score:<8.3f} {issues[:35]}")
        
        results.append({
            "name": alpha["name"],
            "passed": eval_result.passed,
            "status": eval_result.quality_status,
            "score": eval_result.composite_score,
        })
    
    print("-" * 75)
    
    passed_count = sum(1 for r in results if r["passed"])
    optimize_count = sum(1 for r in results if r["status"] == "OPTIMIZE")
    
    return {
        "total_alphas": len(results),
        "passed": passed_count,
        "can_optimize": optimize_count,
        "rejected": len(results) - passed_count - optimize_count,
    }


def simulate_pattern_retrieval():
    """模拟模式检索效果"""
    from backend.agents.knowledge_seed import get_patterns_for_dataset_category, ALPHA_101_PATTERNS
    
    categories = ["pv", "analyst", "fundamental", "news", "other"]
    
    print("\n[SIM] Pattern Retrieval Test:")
    print("-" * 50)
    
    results = {}
    for cat in categories:
        patterns = get_patterns_for_dataset_category(cat)
        results[cat] = len(patterns)
        print(f"  * {cat:<15} -> {len(patterns):>3} patterns")
    
    print(f"\n  Total 101-Alpha patterns: {len(ALPHA_101_PATTERNS)}")
    print("-" * 50)
    
    return results


def simulate_diversity_exploration():
    """模拟多样性探索效果"""
    from backend.diversity_tracker import DiversityTracker, ExplorationRecord
    from backend.alpha_semantic_validator import get_known_operators
    
    tracker = DiversityTracker()
    # Use sync fallback for testing
    tracker.available_operators = get_known_operators()
    
    # 模拟已经尝试的组合
    tried_datasets = ["pv6", "pv6", "pv6", "analyst15", "analyst15"]
    tried_operators = [["ts_rank", "ts_delta"], ["ts_rank", "ts_zscore"], ["rank", "ts_delta"]]
    
    for ds in tried_datasets:
        for ops in tried_operators:
            record = ExplorationRecord(
                dataset_id=ds,
                region="USA",
                universe="TOP3000",
                operators_used=ops,
                was_successful=False
            )
            tracker.record_attempt(record)
    
    # 评估不同组合的多样性
    test_combos = [
        ("pv6", ["ts_rank", "ts_delta"], "Repeated"),
        ("fundamental1", ["ts_rank", "ts_delta"], "New Dataset"),
        ("pv6", ["group_neutralize", "vec_avg"], "New Operators"),
        ("news5", ["ts_corr", "ts_std_dev"], "All New"),
    ]
    
    print("\n[SIM] Diversity Exploration:")
    print("-" * 65)
    print(f"{'Description':<15} {'Dataset':<15} {'Diversity':<12} {'Suggestion'}")
    print("-" * 65)
    
    results = []
    for dataset, operators, desc in test_combos:
        score = tracker.evaluate_diversity(
            dataset_id=dataset,
            fields=[],
            operators=operators
        )
        
        suggestion = "Continue" if score.overall_score > 0.6 else "Consider change" if score.overall_score > 0.3 else "Change strongly recommended"
        print(f"{desc:<15} {dataset:<15} {score.overall_score:<12.2f} {suggestion}")
        
        results.append({
            "desc": desc,
            "diversity": score.overall_score,
        })
    
    print("-" * 65)
    
    # 获取探索建议
    suggestions = tracker.get_exploration_suggestions(n=3)
    if suggestions:
        print("\n[TIP] Exploration Suggestions:")
        for s in suggestions:
            print(f"  * [{s.dimension}] {s.suggestion}")
            if s.underexplored_items:
                print(f"    Recommended: {', '.join(s.underexplored_items[:5])}")
    
    return results


# =============================================================================
# Database Tests
# =============================================================================

async def check_knowledge_base_db():
    """检查数据库中的知识库状态"""
    try:
        from backend.database import AsyncSessionLocal
        from backend.models import KnowledgeEntry
        from sqlalchemy import select, func
        
        async with AsyncSessionLocal() as session:
            # 统计条目数
            result = await session.execute(
                select(
                    KnowledgeEntry.entry_type,
                    func.count(KnowledgeEntry.id).label('count')
                ).where(
                    KnowledgeEntry.is_active == True
                ).group_by(KnowledgeEntry.entry_type)
            )
            
            type_counts = dict(result.fetchall())
            
            # 获取来源分布
            result = await session.execute(
                select(func.count(KnowledgeEntry.id)).where(
                    KnowledgeEntry.is_active == True
                )
            )
            total = result.scalar() or 0
            
            return {
                "total_entries": total,
                "success_patterns": type_counts.get('SUCCESS_PATTERN', 0),
                "failure_pitfalls": type_counts.get('FAILURE_PITFALL', 0),
                "status": "OK" if total > 0 else "EMPTY"
            }
            
    except Exception as e:
        return {
            "status": "ERROR",
            "error": str(e)
        }


async def seed_knowledge_base_if_empty():
    """如果知识库为空，则初始化"""
    try:
        from backend.agents.knowledge_seed import seed_knowledge_base
        
        result = await seed_knowledge_base(force_reseed=False)
        
        if isinstance(result, int):
            if result > 0:
                return {"status": "SEEDED", "entries_added": result}
            else:
                return {"status": "ALREADY_SEEDED", "message": "知识库已有数据"}
        else:
            return {"status": "OK", "entries": result}
            
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


# =============================================================================
# Main Test Runner
# =============================================================================

async def run_full_benchmark():
    """运行完整的基准测试"""
    report = BenchmarkReport()
    
    print("\n" + "=" * 70)
    print("[START] Alpha Mining System - Effectiveness Test")
    print("=" * 70)
    
    # 1. 组件测试
    print("\n[1/4] Testing core components...")
    
    component_tests = [
        ("Knowledge Seed", test_knowledge_seed),
        ("RAG Service", test_rag_service),
        ("Adaptive Thresholds", test_adaptive_thresholds),
        ("Diversity Tracker", test_diversity_tracker),
        ("Feedback Agent", test_feedback_agent),
        ("Metrics Tracker", test_metrics_tracker),
    ]
    
    for name, test_func in component_tests:
        try:
            status, message, details = test_func()
            report.add_component(name, status, message, details)
        except Exception as e:
            report.add_component(name, "ERROR", str(e))
    
    # 2. 数据库检查
    print("\n[2/4] Checking knowledge base database...")
    
    kb_status = await check_knowledge_base_db()
    if kb_status.get("status") == "EMPTY":
        print("  [WARN] Knowledge base empty, initializing...")
        seed_result = await seed_knowledge_base_if_empty()
        kb_status = await check_knowledge_base_db()
        print(f"  [OK] KB initialized: {kb_status.get('total_entries', 0)} entries")
    elif kb_status.get("status") == "ERROR":
        print(f"  [ERR] Database connection failed: {kb_status.get('error')}")
    else:
        print(f"  [OK] KB normal: {kb_status.get('total_entries', 0)} entries")
    
    report.knowledge_base = kb_status
    
    # 3. 模拟测试
    print("\n[3/4] Running simulation tests...")
    
    # Alpha评估模拟
    eval_results = simulate_alpha_evaluation()
    report.simulation_results["alpha_evaluation"] = eval_results
    
    # 模式检索模拟
    retrieval_results = simulate_pattern_retrieval()
    report.simulation_results["pattern_retrieval"] = retrieval_results
    
    # 多样性探索模拟
    diversity_results = simulate_diversity_exploration()
    report.simulation_results["diversity_exploration"] = diversity_results
    
    # 4. 改进评估
    print("\n[4/4] Generating improvement assessment...")
    
    # 计算改进指标
    ok_count = sum(1 for c in report.components if c.status == "OK")
    total_count = len(report.components)
    
    report.improvement_assessment = {
        "Component Health": f"{ok_count}/{total_count} ({ok_count/total_count:.0%})",
        "Knowledge Base": f"{kb_status.get('total_entries', 0)} entries",
        "Category Coverage": f"{len(retrieval_results)} categories",
        "Alpha Pass Rate (sim)": f"{eval_results['passed']}/{eval_results['total_alphas']}",
        "Optimizable Alphas": f"{eval_results['can_optimize']}",
    }
    
    # 打印报告
    report.print_report()
    
    # 给出建议
    print("\n[TIPS] Recommendations:")
    print("-" * 50)
    
    if kb_status.get("total_entries", 0) < 50:
        print("  1. KB entries low, run: python -m backend.agents.knowledge_seed --force")
    
    if ok_count < total_count:
        failed = [c.name for c in report.components if c.status != "OK"]
        print(f"  2. Components need attention: {', '.join(failed)}")
    
    if eval_results["passed"] == 0:
        print("  3. Simulated pass rate is 0, may need threshold or pattern adjustment")
    
    print("\n  For actual mining test, ensure:")
    print("    - Database configured (.env file)")
    print("    - BRAIN credentials set")
    print("    - Run: python -m backend.main to start service")
    
    return report


async def run_quick_check():
    """快速检查各组件状态"""
    print("\n" + "=" * 50)
    print("[QUICK] Component Status Check")
    print("=" * 50 + "\n")
    
    tests = [
        ("Knowledge Seed", test_knowledge_seed),
        ("RAG Service", test_rag_service),
        ("Adaptive Thresholds", test_adaptive_thresholds),
        ("Diversity Tracker", test_diversity_tracker),
        ("Feedback Agent", test_feedback_agent),
        ("Metrics Tracker", test_metrics_tracker),
    ]
    
    ok_count = 0
    for name, test_func in tests:
        try:
            status, message, _ = test_func()
            icon = "[OK]" if status == "OK" else "[WARN]" if status == "WARNING" else "[ERR]"
            print(f"{icon} {name}: {message}")
            if status == "OK":
                ok_count += 1
        except Exception as e:
            print(f"[ERR] {name}: {e}")
    
    print(f"\nResult: {ok_count}/{len(tests)} components OK")
    print("=" * 50)


async def seed_only():
    """仅初始化知识库"""
    print("\n[SEED] Initializing knowledge base...")
    
    try:
        from backend.agents.knowledge_seed import seed_knowledge_base
        
        result = await seed_knowledge_base(force_reseed=True)
        print(f"[OK] Knowledge base initialized: {result} entries")
        
    except Exception as e:
        print(f"[ERR] Initialization failed: {e}")
        print("\nTip: Ensure database is properly configured (.env file)")


def main():
    parser = argparse.ArgumentParser(description="Alpha Mining System 效果测试")
    parser.add_argument("--full", action="store_true", help="运行完整测试")
    parser.add_argument("--quick", action="store_true", help="快速检查")
    parser.add_argument("--seed", action="store_true", help="仅初始化知识库")
    
    args = parser.parse_args()
    
    if args.seed:
        asyncio.run(seed_only())
    elif args.quick:
        asyncio.run(run_quick_check())
    else:
        # 默认运行完整测试
        asyncio.run(run_full_benchmark())


if __name__ == "__main__":
    main()
