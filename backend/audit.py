"""Read-only, reproducible checks of all input rows; no roles or final exports."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import platform
import time
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
AMOUNT_ATOL = 1e-6
AMOUNT_RTOL = 1e-12
EXPECTED_TYPES = {
    "nodes": {"gid": pa.int64(), "depth": pa.int64(), "is_seed": pa.bool_()},
    "edges": {
        "src": pa.int64(), "dst": pa.int64(), "sum_kzt": pa.float64(),
        "n_tx": pa.int64(), "depth": pa.int8(),
    },
    "transactions": {
        "src": pa.int64(), "dst": pa.int64(), "date": pa.date32(),
        "sum_kzt": pa.float64(),
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_valid_audit(report: dict[str, Any]) -> None:
    """Reject malformed input before any dependent computation/API response."""
    failed = [check["name"] for check in report["checks"] if not check["passed"]]
    if failed:
        raise ValueError("Проверка данных не пройдена: " + "; ".join(failed))


def audit_data(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Read all three parquet files, validate values and derive actual summary.

    The returned summary has no IDs; any reported ID bounds are exact strings.
    This function never writes input files or removes duplicate transactions.
    """
    started = time.perf_counter()
    data_dir = Path(data_dir)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir.resolve()),
        "versions": {"python": platform.python_version(), "pandas": pd.__version__,
                     "pyarrow": pa.__version__, "networkx": nx.__version__},
        "summary": {}, "schemas": {}, "null_counts": {}, "hashes": {},
        "file_sizes": {}, "checks": [], "statistics": {},
    }

    def check(name: str, passed: bool, details: str = "") -> None:
        report["checks"].append({"name": name, "passed": bool(passed), "details": details})

    def finish() -> dict[str, Any]:
        for name, before in report["hashes"].items():
            check(f"{name}: файл не изменился за время аудита",
                  _sha256(data_dir / f"{name}.parquet") == before)
        report["elapsed_seconds"] = time.perf_counter() - started
        return report

    frames: dict[str, pd.DataFrame] = {}
    for name, expected in EXPECTED_TYPES.items():
        path = data_dir / f"{name}.parquet"
        report["hashes"][name] = _sha256(path)
        report["file_sizes"][name] = path.stat().st_size
        table = pq.read_table(path)  # Reads every row and column, not just metadata.
        report["schemas"][name] = {field.name: str(field.type) for field in table.schema}
        report["null_counts"][name] = {
            column: table.column(column).null_count for column in table.column_names
        }
        schema_ok = set(table.column_names) == set(expected) and all(
            field in table.column_names and table.schema.field(field).type == dtype
            for field, dtype in expected.items()
        )
        check(f"{name}: схема и типы", schema_ok, str(report["schemas"][name]))
        check(f"{name}: нет пропусков", all(n == 0 for n in report["null_counts"][name].values()),
              str(report["null_counts"][name]))
        check(f"{name}: таблица непустая", table.num_rows > 0, f"строк={table.num_rows}")
        frames[name] = table.to_pandas()
    if any(not item["passed"] for item in report["checks"]):
        return finish()

    nodes, edges, tx = (frames[name] for name in ("nodes", "edges", "transactions"))
    dates = pd.to_datetime(tx["date"], errors="coerce")
    summary = {
        "node_count": len(nodes), "edge_count": len(edges), "transaction_count": len(tx),
        "seed_count": int(nodes["is_seed"].sum()),
        "period": {"start": dates.min().date().isoformat(), "end": dates.max().date().isoformat()},
    }
    report["summary"] = summary
    node_ids = set(nodes["gid"])
    seed_ids = set(nodes.loc[nodes["is_seed"], "gid"])
    check("nodes: gid уникальны", nodes["gid"].is_unique)
    check("edges: пары src/dst уникальны", not edges.duplicated(["src", "dst"]).any())
    for name, frame in (("edges", edges), ("transactions", tx)):
        unknown = (set(frame["src"]) | set(frame["dst"])) - node_ids
        check(f"{name}: все src/dst есть в nodes", not unknown, f"неизвестных ID={len(unknown)}")
        check(f"{name}: суммы конечные и положительные",
              bool(np.isfinite(frame["sum_kzt"]).all() and frame["sum_kzt"].gt(0).all()))
    check("nodes: depth в диапазоне 0–4", nodes["depth"].between(0, 4).all())
    check("nodes: is_seed эквивалентен depth=0", nodes["is_seed"].eq(nodes["depth"].eq(0)).all())
    check("nodes: есть исходные seed", bool(seed_ids))
    check("edges: depth в диапазоне 1–4", edges["depth"].between(1, 4).all())
    check("edges: n_tx положительные целые", edges["n_tx"].gt(0).all())
    check("transactions: порог >= 5000 KZT", tx["sum_kzt"].ge(5000).all())
    check("transactions: даты в заявленном июле 2026",
          dates.notna().all() and dates.between("2026-07-01", "2026-07-31").all(),
          f"{summary['period']['start']} — {summary['period']['end']}; уникальных дат={dates.nunique()}")
    if any(not item["passed"] for item in report["checks"]):
        return finish()

    # Duplicate rows remain in the aggregation: there is no transaction_id.
    aggregated = tx.groupby(["src", "dst"], sort=True, as_index=False).agg(
        expected_sum=("sum_kzt", "sum"), expected_n_tx=("sum_kzt", "size")
    )
    merged = edges.merge(aggregated, on=["src", "dst"], how="outer", indicator=True,
                         validate="one_to_one")
    pairs_match = bool(merged["_merge"].eq("both").all())
    check("Агрегация: набор направленных пар совпадает", pairs_match,
          f"только edges={int(merged['_merge'].eq('left_only').sum())}; "
          f"только transactions={int(merged['_merge'].eq('right_only').sum())}")
    sum_match = np.isclose(merged["sum_kzt"], merged["expected_sum"],
                           atol=AMOUNT_ATOL, rtol=AMOUNT_RTOL, equal_nan=False)
    check("Агрегация: сумма совпадает у каждой пары", pairs_match and sum_match.all(),
          f"несовпадений={int((~sum_match).sum())}; atol={AMOUNT_ATOL}, rtol={AMOUNT_RTOL}")
    counts_match = merged["n_tx"].eq(merged["expected_n_tx"])
    check("Агрегация: n_tx совпадает у каждой пары", pairs_match and counts_match.all(),
          f"несовпадений={int((~counts_match).sum())}; сравнение точное")
    tx_total = float(tx["sum_kzt"].sum())
    edge_total = float(edges["sum_kzt"].sum())
    check("Агрегация: общий оборот совпадает",
          np.isclose(edge_total, tx_total, atol=AMOUNT_ATOL, rtol=AMOUNT_RTOL),
          f"edges={edge_total:.8f}; transactions={tx_total:.8f} KZT")
    check("Агрегация: общее число переводов совпадает", int(edges["n_tx"].sum()) == len(tx),
          f"sum(edges.n_tx)={int(edges['n_tx'].sum())}; len(transactions)={len(tx)}")

    graph = nx.DiGraph()
    graph.add_nodes_from(nodes["gid"].tolist())
    graph.add_edges_from(edges[["src", "dst"]].itertuples(index=False, name=None))
    isolated = set(nx.isolates(graph))
    check("Полный граф содержит все входные узлы", set(graph) == node_ids,
          f"узлов={graph.number_of_nodes()}; изолированных={len(isolated)}")
    distances = nx.multi_source_dijkstra_path_length(graph, seed_ids, weight=None)
    depth_mismatches = sum(distances.get(row.gid) != row.depth for row in nodes.itertuples(index=False))
    check("nodes: depth равен кратчайшему пути от seed", depth_mismatches == 0,
          f"несовпадений={depth_mismatches}; недостижимых={len(node_ids - set(distances))}")
    source_depth = edges["src"].map(nodes.set_index("gid")["depth"])
    check("edges: depth равен depth(src)+1", edges["depth"].eq(source_depth + 1).all())
    boundary_ids = set(nodes.loc[nodes["depth"].eq(4), "gid"])
    boundary_no_out = sum(graph.out_degree(gid) == 0 for gid in boundary_ids)
    check("Граница depth=4 не имеет наблюдаемых исходящих", boundary_no_out == len(boundary_ids),
          f"depth=4: {len(boundary_ids)}; без исходящих: {boundary_no_out}")

    components = sorted(nx.weakly_connected_components(graph), key=lambda group: (-len(group), min(group)))
    connected_components = [group for group in components if not group.issubset(isolated)]
    in_amount = edges.groupby("dst")["sum_kzt"].sum().reindex(nodes["gid"], fill_value=0.0)
    out_amount = edges.groupby("src")["sum_kzt"].sum().reindex(nodes["gid"], fill_value=0.0)
    ratio = out_amount.div(in_amount.where(in_amount.gt(0)))
    report["statistics"] = {
        "depth_counts": {str(depth): int(count) for depth, count in nodes["depth"].value_counts().sort_index().items()},
        "edge_depth_counts": {str(depth): int(count) for depth, count in edges["depth"].value_counts().sort_index().items()},
        "transaction_sum_kzt": tx_total,
        "edge_sum_kzt": edge_total,
        "edge_n_tx_total": int(edges["n_tx"].sum()),
        "min_transaction_kzt": float(tx["sum_kzt"].min()),
        "max_transaction_kzt": float(tx["sum_kzt"].max()),
        "unique_dates": int(dates.nunique()),
        "duplicate_transaction_rows_excluding_first": int(tx.duplicated().sum()),
        "rows_in_duplicate_groups": int(tx.duplicated(keep=False).sum()),
        "duplicate_groups": int(tx.loc[tx.duplicated(keep=False)].drop_duplicates().shape[0]),
        "maximum_pair_sum_difference_kzt": float((merged["sum_kzt"] - merged["expected_sum"]).abs().max()),
        "isolated_nodes": len(isolated), "isolated_seed_nodes": len(isolated & seed_ids),
        "seed_receiver_only": sum(graph.in_degree(gid) > 0 and graph.out_degree(gid) == 0 for gid in seed_ids),
        "seed_without_outgoing": sum(graph.out_degree(gid) == 0 for gid in seed_ids),
        "boundary_nodes": len(boundary_ids), "boundary_without_outgoing": boundary_no_out,
        "weak_components_all_nodes": len(components),
        "weak_components_edge_nodes_only": len(connected_components),
        "component_sizes_and_seeds": [{"nodes": len(group), "seeds": len(group & seed_ids)} for group in components],
        "nodes_outside_largest_component": len(nodes) - len(components[0]),
        "edge_nodes_outside_largest_component": len(nodes) - len(isolated) - len(components[0]),
        "outgoing_greater_than_incoming_nodes": int(out_amount.gt(in_amount).sum()),
        "outgoing_greater_with_positive_incoming_nodes": int((out_amount.gt(in_amount) & in_amount.gt(0)).sum()),
        "outgoing_with_zero_incoming_nodes": int((out_amount.gt(0) & in_amount.eq(0)).sum()),
        "ratio_0_8_to_1_2_nodes": int(ratio.between(0.8, 1.2).sum()),
        "self_loops": nx.number_of_selfloops(graph),
        "min_gid": str(nodes["gid"].min()), "max_gid": str(nodes["gid"].max()),
        "ids_above_js_safe_integer": sum(abs(gid) > 2**53 - 1 for gid in node_ids),
    }
    return finish()


def render_markdown(report: dict[str, Any]) -> str:
    """Render factual results and case caveats without inventing computed roles."""
    passed = all(item["passed"] for item in report["checks"])
    lines = [
        "# Аудит исходных данных Fusion", "",
        f"Результат: **{'ПРОЙДЕН' if passed else 'ОШИБКИ'}**. Все три parquet прочитаны целиком локально.",
        f"Время проверки с чтением и SHA-256: {report['elapsed_seconds']:.3f} с; запуск UTC: {report['generated_at']}.",
        "", "Воспроизведение из корня проекта:", "", "```bash",
        ".venv/bin/python -m backend.audit", "```", "",
        "Скрипт обновляет этот отчёт; при ошибках проверки завершается с кодом 1. "
        "Исходные файлы не изменяются; повторные транзакции участвуют в суммах и счётчиках.",
        "", "Версии: " + ", ".join(f"{key} {value}" for key, value in report["versions"].items()) + ".", "",
        "## Файлы и схемы", "", "| Файл | Строк | Размер, байт | Поля Arrow |", "|---|---:|---:|---|",
    ]
    summary = report["summary"]
    count_keys = {"nodes": "node_count", "edges": "edge_count", "transactions": "transaction_count"}
    for name, schema in report["schemas"].items():
        columns = ", ".join(f"`{key}: {value}`" for key, value in schema.items())
        lines.append(f"| {name}.parquet | {summary.get(count_keys[name], 'см. проверки')} | {report['file_sizes'][name]} | {columns} |")
    if report["statistics"]:
        s = report["statistics"]
        lines.extend([
            "", "## Фактический результат", "",
            f"- Узлы / рёбра / транзакции: **{summary['node_count']} / {summary['edge_count']} / {summary['transaction_count']}**; seed: **{summary['seed_count']}**.",
            f"- Период: **{summary['period']['start']} — {summary['period']['end']}**, уникальных дат: {s['unique_dates']}.",
            "- Распределение узлов по depth: " + ", ".join(f"{key}: {value}" for key, value in s["depth_counts"].items()) + ".",
            "- Распределение рёбер по depth: " + ", ".join(f"{key}: {value}" for key, value in s["edge_depth_counts"].items()) + ".",
            f"- Оборот: **{s['transaction_sum_kzt']:.2f} KZT**; диапазон отдельного перевода: {s['min_transaction_kzt']:.2f}–{s['max_transaction_kzt']:.2f} KZT.",
            f"- Сумма n_tx: {s['edge_n_tx_total']}; наибольшее расхождение суммы отдельной пары: {s['maximum_pair_sum_difference_kzt']:.12g} KZT.",
            f"- Повторных строк сверх первого экземпляра: **{s['duplicate_transaction_rows_excluding_first']}**; в повторяющихся группах всего {s['rows_in_duplicate_groups']} строк / {s['duplicate_groups']} групп. Без transaction_id это не основание для удаления; все строки сохранены при расчёте.",
            f"- Изолированные узлы: **{s['isolated_nodes']}**, из них seed: {s['isolated_seed_nodes']}. Seed только с входящими: {s['seed_receiver_only']}; seed без исходящих всего: {s['seed_without_outgoing']}.",
            f"- Граница depth=4: **{s['boundary_nodes']}** узла; без исходящих: {s['boundary_without_outgoing']}. Это неполнота наблюдения, а не основание назначить terminal.",
            f"- Слабосвязные компоненты: **{s['weak_components_all_nodes']}** со всеми узлами; **{s['weak_components_edge_nodes_only']}** при построении только по рёбрам.",
            "- Размер / seed каждой компоненты, по убыванию размера: " + ", ".join(f"{item['nodes']}/{item['seeds']}" for item in s["component_sizes_and_seeds"]) + ".",
            f"- Вне крупнейшей компоненты: **{s['nodes_outside_largest_component']}** узел с изолированными / **{s['edge_nodes_outside_largest_component']}** только среди концов рёбер.",
            f"- Наблюдаемый исходящий оборот выше входящего у **{s['outgoing_greater_than_incoming_nodes']}** узлов: у {s['outgoing_greater_with_positive_incoming_nodes']} при in>0 и у {s['outgoing_with_zero_incoming_nodes']} при in=0. Отношение out/in в [0.8; 1.2] при in>0 — у {s['ratio_0_8_to_1_2_nodes']}. Это проверка справочных чисел, без присвоения ролей.",
            f"- Петель src=dst: {s['self_loops']}. Диапазон gid: `{s['min_gid']}`–`{s['max_gid']}`; выше безопасного целого JavaScript: {s['ids_above_js_safe_integer']} ID. В parquet/Python — int64/точное целое, в JSON — строки.",
        ])
    lines.extend(["", "## Проверки", "", "| Проверка | Статус | Детали |", "|---|---|---|"])
    for item in report["checks"]:
        detail = item["details"].replace("|", "\\|")
        lines.append(f"| {item['name']} | {'OK' if item['passed'] else 'FAIL'} | {detail} |")
    lines.extend([
        "", f"Для сумм используется `abs(actual - expected) <= {AMOUNT_ATOL} + {AMOUNT_RTOL} * abs(expected)` KZT; "
        "n_tx и наборы направленных пар сравниваются точно. ID никогда не проходят через float или JavaScript Number.",
        "", "## Сверка материалов и ограничения", "",
        "Скрипт не фиксирует размер данных как алгоритмическую константу. Числа ниже — заявления материалов, "
        "которые сопоставляются с пересчитанными значениями, а не подставляются в результаты.", "",
    ])
    if report["statistics"]:
        s = report["statistics"]
        claims = [
            ("Узлы / рёбра / транзакции", "2248 / 3119 / 4840", f"{summary['node_count']} / {summary['edge_count']} / {summary['transaction_count']}"),
            ("Seed / depth 0–4", "81 / 81,472,462,789,444", f"{summary['seed_count']} / " + ",".join(str(s['depth_counts'].get(str(i), 0)) for i in range(5))),
            ("Период", "2026-07-01 — 2026-07-31", f"{summary['period']['start']} — {summary['period']['end']}"),
            ("Оборот DATA_NOTES / округление ТЗ", "365890012.01 / 365890012 KZT", f"{s['transaction_sum_kzt']:.2f} KZT"),
            ("Минимальная транзакция", "5000 KZT", f"{s['min_transaction_kzt']:.2f} KZT"),
            ("Повторные строки сверх первого экземпляра", "97", str(s['duplicate_transaction_rows_excluding_first'])),
            ("Изоляты / только входящие seed / seed без исходящих", "19 / 12 / 31", f"{s['isolated_nodes']} / {s['seed_receiver_only']} / {s['seed_without_outgoing']}"),
            ("Depth=4 без исходящих", "444", str(s['boundary_without_outgoing'])),
            ("Компоненты только по рёбрам / со всеми узлами", "16 / 35", f"{s['weak_components_edge_nodes_only']} / {s['weak_components_all_nodes']}"),
            ("out > in", "354 (без оговорки in>0)", f"{s['outgoing_greater_than_incoming_nodes']} всего; {s['outgoing_greater_with_positive_incoming_nodes']} при in>0"),
            ("out/in в [0.8;1.2]", "72", str(s['ratio_0_8_to_1_2_nodes'])),
        ]
        lines.extend(["| Показатель | В материалах | Пересчитано |", "|---|---|---|"])
        lines.extend(f"| {label} | {claim} | {observed} |" for label, claim, observed in claims)
    lines.extend([
        "", "1. **Компоненты:** 16 и 352 узла вне крупнейшей компоненты в ТЗ относятся к графу только по рёбрам. Полный граф обязан включать изолированные seed; фактические числа полного графа приведены выше. Это различие состава графа, не повод терять узлы.",
        "2. **Оборот:** ТЗ округляет до целых KZT, DATA_NOTES сохраняет тиыны; расчёт не округляет отдельные транзакции.",
        "3. **Восемь сообществ:** заявление о базовом Louvain не задаёт воспроизводимые параметры и не является целевым числом кластеров. На этом этапе кластеризация не запускалась.",
        "4. **Скор роли:** в ТЗ назван уверенностью, но разметки нет. В следующем этапе это будет эвристическая поддержка гипотезы, без accuracy и вероятности виновности; coordinator — структурная гипотеза.",
        "5. **Сроки:** ТЗ описывает 5 часов и 5-минутное демо; текущий запрос задаёт бюджет 3–3.5 часа и только первый этап. PLAN предлагает 3-минутную основную демонстрацию с резервом.",
        "6. **Обрыв на depth=4:** в опциональном разделе ТЗ выделен отдельно, но текущие инструкции требуют учитывать его обязательно. Входящий поток seed неполон, а суммы всей сети отражают только выгрузку, не полный баланс счетов.",
        "7. **Starter:** строит граф только по рёбрам, sanity_check сверяет набор пар без значений сумм/n_tx, а requirements не содержит scipy для PageRank. README говорит, что transactions не использован, хотя код читает его и группирует для sanity_check; временные признаки действительно не вычисляются. Starter сохранён без изменений и не запускался для фиктивных CSV.",
        "8. **out > in:** число 354 из ТЗ воспроизводится только с дополнительным условием in>0. Узлы с нулевым наблюдаемым входящим и положительным исходящим тоже удовлетворяют out>in; их исключение должно быть явным. Это не расхождение из-за погрешности float.",
        "", "Обязательный итог будущего MVP: nodes_roles.csv для каждого входного узла (шесть ролей, конечные скоры [0,1], "
        "evidence ≤200 символов с конкретными признаками); clusters.csv для всех узлов, включая одиночные; top_nodes.csv "
        "не менее 20 узлов; направленный граф, поиск любого gid, карточка и скачивание CSV; воспроизводимый расчёт ≤5 минут, "
        "README с правилами/ограничениями/масштабированием и архитектурной схемой. Эти результаты не создаются на этапе 1.",
        "", "## SHA-256 исходных файлов", "",
        "Хеш вычисляется до чтения и повторно после проверки; совпадение проверяется выше. Для сверки неизменности "
        "между запусками сравнивайте с сохранённым отчётом.", "", "| Файл | SHA-256 |", "|---|---|",
    ])
    lines.extend(f"| {name}.parquet | `{digest}` |" for name, digest in report["hashes"].items())
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Полная локальная проверка parquet без изменения данных")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "docs" / "DATA_AUDIT.md")
    args = parser.parse_args()
    try:
        report = audit_data(args.data)
    except (OSError, ValueError, pa.ArrowException) as error:
        print(f"Ошибка чтения данных: {error}")
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_markdown(report), encoding="utf-8")
    try:
        assert_valid_audit(report)
    except ValueError as error:
        print(error)
        print(f"Отчёт: {args.report}")
        return 1
    summary = report["summary"]
    print(f"OK: {summary['node_count']} узлов, {summary['edge_count']} рёбер, "
          f"{summary['transaction_count']} транзакций; "
          f"{summary['period']['start']} — {summary['period']['end']}")
    print(f"Проверок: {len(report['checks'])}; время: {report['elapsed_seconds']:.3f} с")
    print(f"Отчёт: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
