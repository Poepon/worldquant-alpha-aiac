"""Phase 1b B2 — candidate_queue row -> MiningState hydration tests."""
from backend.models import CandidateQueue
from backend.pool.hydrate import hydrate_candidate_state, hg_run_config


def test_hydrate_s_path_empty_sim_result():
    row = CandidateQueue(
        task_id=42, region="USA", universe="TOP3000", delay=1,
        dataset_id="pv1", dataset_category="price_volume",
        expression="ts_rank(close, 20)",
        effective_default_test_period="P0Y", effective_sharpe_submit_min=1.58,
        current_hypothesis_id=7, rag_ab_arm="category",
        context={"hypothesis": "h1", "patterns": [{"x": 1}],
                 "cognitive_layer_id_used": "macro_top_down"},
        sim_result=None,  # S path — not simulated yet
    )
    snap = {"brain_role_snapshot": {
        "brain_consultant_mode_at_start": True,
        "effective_region_universes": {"USA": ["TOP3000"]},
    }}
    st = hydrate_candidate_state(row, snap)

    assert st.task_id == 42 and st.region == "USA" and st.delay == 1
    assert st.dataset_id == "pv1" and st.dataset_category == "price_volume"
    assert len(st.pending_alphas) == 1
    c = st.pending_alphas[0]
    assert c.expression == "ts_rank(close, 20)" and c.is_valid is True
    assert c.metrics == {} and c.is_simulated is False
    # role-snapshot first-class cols (终审 #7)
    assert st.effective_default_test_period == "P0Y"
    assert st.effective_sharpe_submit_min == 1.58
    assert st.brain_consultant_mode_at_start is True
    # lineage scalar + list (LangGraph scalar-drop resilience)
    assert st.current_hypothesis_id == 7 and st.current_hypothesis_ids == [7]
    assert st.rag_ab_arm == "category"
    # buffered HG context
    assert st.patterns == [{"x": 1}]
    assert st.cognitive_layer_id_used == "macro_top_down"


def test_hydrate_e_path_sim_result_becomes_candidate_metrics():
    row = CandidateQueue(
        task_id=42, region="USA", expression="x",
        sim_result={"sharpe": 1.4, "fitness": 0.9, "alpha_id": "ABC123"},
        verdict=None,
    )
    st = hydrate_candidate_state(row, None)
    c = st.pending_alphas[0]
    assert c.metrics == {"sharpe": 1.4, "fitness": 0.9, "alpha_id": "ABC123"}
    assert c.is_simulated is True
    assert c.simulation_success is True
    assert c.alpha_id == "ABC123"
    # universe/delay fall back to defaults when the row left them NULL
    assert st.universe == "TOP3000" and st.delay == 1


def test_hydrate_no_hypothesis_id_gives_empty_lineage_list():
    row = CandidateQueue(task_id=1, region="USA", expression="x",
                         current_hypothesis_id=None)
    st = hydrate_candidate_state(row, None)
    assert st.current_hypothesis_id is None
    assert st.current_hypothesis_ids == []


def test_hg_run_config_db_free():
    assert hg_run_config() == {"configurable": {"trace_service": None}}
    sentinel = object()
    assert hg_run_config(sentinel) == {"configurable": {"trace_service": sentinel}}
