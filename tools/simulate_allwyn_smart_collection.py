from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.smart_collection import (
    author_concentration,
    canonical_topic,
    detect_dominant_entity_collisions,
    x_reply_deepening_input,
    x_search_input,
)


def load_csv(path: Path) -> list[dict]:
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def compact(row: dict) -> dict:
    return {
        'id': row.get('id'),
        'text': row.get('text'),
        'authorUsername': row.get('authorUsername') or row.get('author/username'),
        'replyCount': row.get('replyCount'),
        'viewCount': row.get('viewCount'),
        'searchTerm': row.get('searchTerm'),
        'url': row.get('url'),
    }


def main(broad_path: str, refined_path: str, out_path: str):
    broad = [compact(x) for x in load_csv(Path(broad_path))]
    refined = [compact(x) for x in load_csv(Path(refined_path))]

    draft = AnalysisDraft(
        client='Allwyn', topic='Allwyn', market='Greece',
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=['Allwyn'], sources=['x'], sample_mode='automatic', sample_target=500,
        comments=True, max_budget_usd=2.0, smart_search=True, report_language='Ελληνικά',
        additional_context=['OPAP', 'ΟΠΑΠ'], exclusions=[],
    )
    plan = build_collection_plan(draft)
    inp = plan.sources[0].subruns[0].input
    collisions = detect_dominant_entity_collisions(broad, canonical_topic(draft.topic, draft.market))
    dynamic_exclusions = [x['exclude_term'] for x in collisions]
    refined_input, refined_buckets = x_search_input(draft, 500, collision_exclusions=dynamic_exclusions)
    reply_input, reply_seeds = x_reply_deepening_input(refined, requested_items=500 - len(refined), max_seeds=40)

    report = {
        'case': 'Allwyn / Greece / X / 2026-08-15..2026-08-31',
        'broad_discovery_observed': {
            'rows': len(broad),
            'author_concentration': author_concentration(broad),
            'dominant_context_collisions': collisions,
        },
        'smart_collection_v2_first_wave': {
            'strategy_version': plan.search_strategy_version,
            'target_semantics': plan.target_semantics,
            'actor_input': inp,
            'intent_buckets': plan.sources[0].intent_buckets,
            'why': 'Multiple bounded intent searches replace one uncapped broad keyword search.',
        },
        'adaptive_refinement_after_wave_1': {
            'dynamic_exclusions_for_non_property_buckets': dynamic_exclusions,
            'actor_input_preview': refined_input,
            'intent_buckets': refined_buckets,
            'rule': 'Dominant context is split/capped, not erased from evidence.',
        },
        'refined_manual_run_observed': {
            'rows': len(refined),
            'author_concentration': author_concentration(refined),
        },
        'reply_deepening_preview': {
            'available_seed_count_in_refined_sample': len(reply_seeds),
            'seed_preview': reply_seeds[:10],
            'actor_input_preview': reply_input,
            'note': 'This is a plan preview only. No paid reply run was executed by this simulation.',
        },
        'acceptance_interpretation': {
            'what_500_means': 'Target up to 500 analyzable evidence units, not 500 literal keyword hits.',
            'stop_rule': 'Stop at the real relevant supply if the target cannot be reached without lowering relevance.',
            'next_runtime_work': 'Wire adaptive refinement + reply deepening into the collection lifecycle after cleaning, preserving the global budget guard and audit trail.',
        },
    }
    Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('usage: simulate_allwyn_smart_collection.py BROAD.csv REFINED.csv OUT.json')
    main(sys.argv[1], sys.argv[2], sys.argv[3])
