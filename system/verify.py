#!/usr/bin/env python3
"""Validate all outputs and benchmark fresh processes from an unrelated directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from domain import Institution, validate_result

SYSTEM = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=SYSTEM.parent)
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--sample', type=int, default=200)
    args = parser.parse_args()
    if args.repeat < 1 or args.sample < 1:
        parser.error('repeat and sample must be positive')
    workspace = args.workspace.resolve()
    report = {'mode': 'rules', 'accuracy': 'not measured; no reference labels supplied', 'institutions': {}}
    for name in ['bank', 'emoney', 'investment', 'payments_fx']:
        inst = Institution(workspace / 'institutions' / name)
        path = workspace / 'output' / (name + '.jsonl')
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
        indexed = Institution.index(rows, 'case_id')
        if set(indexed) != {c['case_id'] for c in inst.cases}:
            raise ValueError(f'{name}: output case set differs from input')
        for case in inst.cases:
            validate_result(indexed[case['case_id']], inst.context(case))
        checks = []
        # All artifacts remain in system; no change to input data, packages, or outside directories.
        (SYSTEM / 'reports').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='verification ', dir=SYSTEM / 'reports') as temp:
            for seed in range(args.repeat):
                target = Path(temp) / (str(seed) + '.jsonl')
                env = dict(os.environ, PYTHONHASHSEED=str(seed))
                command = [sys.executable, str(SYSTEM / 'run'), str(inst.root), str(target), '--mode', 'rules', '--limit', str(args.sample), '--no-audit']
                completed = subprocess.run(command, cwd=temp, env=env, capture_output=True, text=True, encoding='utf-8', timeout=1800, check=True)
                metrics = json.loads(completed.stderr.strip().splitlines()[-1])
                generated = target.read_bytes()
                parsed = [json.loads(line) for line in generated.decode('utf-8').splitlines()]
                if len(parsed) != min(args.sample, len(inst.cases)):
                    raise ValueError('Benchmark missed a case')
                expected = [indexed[c['case_id']] for c in sorted(inst.cases, key=lambda c:c['case_id'])[:args.sample]]
                if parsed != expected:
                    raise ValueError(f'{name}: saved development outputs do not match a fresh run')
                checks.append({'sha256': hashlib.sha256(generated).hexdigest(), 'seconds': metrics['seconds'], 'cases': metrics['cases'], 'cost_usd': metrics['model_cost_usd']})
                print(f'{name}: fresh run {seed+1}/{args.repeat}, {metrics["cases"]} cases, {metrics["seconds"]:.3f}s', flush=True)
        if len({r['sha256'] for r in checks}) != 1:
            raise ValueError(f'{name}: repeated output bytes differ')
        report['institutions'][name] = {'validated_cases': len(rows), 'fresh_runs': checks, 'byte_identical': True}
    target = SYSTEM / 'reports' / 'verification.json'
    target.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f'PASS: all outputs validated; repeated fresh-process outputs match. Report: {target}', flush=True)


if __name__ == '__main__':
    main()
