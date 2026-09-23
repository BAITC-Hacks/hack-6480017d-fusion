"""Named heuristic choices, not learned thresholds. See docs/METHODOLOGY.md."""
RULES_VERSION = 'fusion-rules-1'
ROLES = ('consolidator', 'transit', 'distributor', 'terminal', 'coordinator', 'peripheral')
MAX_OBSERVED_DEPTH = 4
SEED_REACH_HOPS = 4  # Structural proximity within the observed graph, not tracing specific money.
MIN_CONSOLIDATOR_SENDERS = 3  # Convergence from several distinct observed counterparties.
MIN_DISTRIBUTOR_RECEIVERS = 10  # A visibly broad fan-out rather than a single onward transfer.
MIN_COORDINATOR_SENDERS = 3
MIN_COORDINATOR_RECEIVERS = 2
MIN_COORDINATOR_SEEDS = 2
COORDINATOR_PR_QUANTILE = 0.95  # High weighted incoming prominence plus convergence and fan-out.
TRANSIT_RATIO_LOW = 0.8
TRANSIT_RATIO_HIGH = 1.2  # Tolerance for observed flows, never a complete account balance.
PAGERANK_ALPHA = 0.85
PAGERANK_TOL = 1e-12
PAGERANK_MAX_ITER = 1000
LOUVAIN_SEED = 42
LOUVAIN_RESOLUTION = 1.0
BOUNDARY_PRIORITY_FACTOR = 0.5  # Lower evidential completeness; not an estimated probability.
PRIORITY_WEIGHTS = {'pagerank': 0.30, 'turnover': 0.20, 'seed_reach': 0.25,
                    'counterparties': 0.15, 'transactions': 0.10}
DEFAULT_TOP_N = 20
