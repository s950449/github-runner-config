#!/usr/local/bin/python3
"""Read-only GitHub status using the installed controller's App authentication."""
import argparse
import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys


def summarize_job(core, job, cfg):
    return {**{key: job.get(key) for key in
               ['id', 'run_id', 'status', 'conclusion', 'labels', 'runner_id', 'runner_name']},
            'queued_and_labels_compatible': core.eligible_job(job, cfg)}


def collect_status(core, api, cfg, job_id=0):
    result = {'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'repo': cfg['repo'], 'configured_workflows': cfg['workflows'],
              'runner_labels': cfg['labels'], 'runners': [], 'active_runs': []}
    for runner in api.pages(api.base + '/actions/runners', 'runners'):
        if isinstance(runner, dict):
            result['runners'].append({key: runner.get(key) for key in ['id', 'name', 'status', 'busy']})
    if core.positive(job_id):
        job = api.request('GET', api.base + '/actions/jobs/' + str(job_id))
        result['requested_job'] = summarize_job(core, job, cfg)
    # Include waiting/requested runs too, to expose any statuses not scanned by dispatch.
    response = api.request('GET', api.base + '/actions/runs?per_page=20')
    runs = response.get('workflow_runs') if isinstance(response, dict) else None
    if not isinstance(runs, list):
        raise RuntimeError('Invalid workflow runs response')
    for run in runs:
        if not isinstance(run, dict) or not core.positive(run.get('id')) or run.get('status') == 'completed':
            continue
        item = {key: run.get(key) for key in ['id', 'path', 'event', 'status', 'conclusion']}
        item['matches_start_policy'] = core.eligible_run(run, cfg)
        item['jobs'] = [summarize_job(core, job, cfg) for job in
                        api.pages(api.base + '/actions/runs/' + str(run['id']) + '/jobs', 'jobs')
                        if isinstance(job, dict)]
        result['active_runs'].append(item)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-id', type=int, default=0)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run as root on the OpenBSD host')
    spec = importlib.util.spec_from_file_location('ci_status_core', '/usr/local/libexec/ci-controller.py')
    core = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = core
    spec.loader.exec_module(core)
    cfg = core.validate_config(json.loads(core.read_private(Path('/etc/ci-runner/config.json'), secret=False)))
    report = collect_status(core, core.GitHub(cfg), cfg, args.job_id)
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error_type': type(error).__name__, 'error': str(error)}, ensure_ascii=True))
        raise SystemExit(1)
