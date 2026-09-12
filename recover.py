"""Recover verified, ordinary local project registrations. Python standard library only."""
import argparse
from contextlib import closing
import copy
import datetime as dt
import hashlib
import json
import ntpath
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

VERSION = 1
FIELDS = {'local-projects', 'project-order', 'app-server-project-id-by-legacy-project-id-by-host'}


def normalize(path):
    value = path.replace('/', '\\')
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return ntpath.normpath(value).casefold()


def path_kind(path):
    parts = normalize(path).split('\\')
    if '.chatgpt-projects' in parts:
        return 'cloud_linked'
    if any(parts[i] in {'.codex', '.claude'} and parts[i+1] == 'worktrees' for i in range(len(parts)-1)):
        return 'worktree'
    p = Path(path)
    marker = p / '.git'
    if marker.is_file():
        # A submodule can also have a gitfile. commondir specifically identifies a linked worktree.
        content = marker.read_text(encoding='utf-8').strip()
        if content.startswith('gitdir:'):
            gitdir = Path(content[7:].strip())
            gitdir = gitdir if gitdir.is_absolute() else p / gitdir
            if (gitdir / 'commondir').is_file():
                return 'worktree'
    return 'directory'


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_state(home):
    target = home / '.codex-global-state.json'
    raw = target.read_bytes()
    data = json.loads(raw)
    for key, typ in [('local-projects', dict), ('project-order', list),
                     ('app-server-project-id-by-legacy-project-id-by-host', dict)]:
        if key not in data or not isinstance(data[key], typ):
            raise ValueError('Unsupported desktop state schema: ' + key)
    for key, p in data['local-projects'].items():
        if not isinstance(p, dict) or p.get('id') != key or not isinstance(p.get('rootPaths'), list):
            raise ValueError('Malformed existing project: ' + key)
    return raw, data


def backend(home):
    path = home / 'state_5.sqlite'
    with path.open('rb') as f:
        if f.read(16) != b'SQLite format 3\x00':
            raise ValueError('Unsupported backend format; expected SQLite')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        for table, required in {
            'projects': {'id','name','created_at_ms','updated_at_ms'},
            'project_roots': {'project_id','position','path'},
            'project_idempotency_keys': {'key','project_id'},
        }.items():
            columns = {r['name'] for r in c.execute('PRAGMA table_info(' + table + ')')}
            if not required <= columns:
                raise ValueError('Unsupported backend schema: ' + table)
        if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
        projects = [dict(r) for r in c.execute('SELECT id,name,created_at_ms,updated_at_ms FROM projects')]
        roots = [dict(r) for r in c.execute('SELECT project_id,position,path FROM project_roots ORDER BY project_id,position')]
        aliases = [dict(r) for r in c.execute('SELECT key,project_id FROM project_idempotency_keys')]
    return projects, roots, aliases


def candidates(home, state):
    projects, roots, aliases = backend(home)
    existing = state['local-projects']
    host_keys = list(state['app-server-project-id-by-legacy-project-id-by-host'])
    hosts = [h for h in host_keys if h.startswith('local:') and normalize(h[6:]) == normalize(str(home))]
    if len(hosts) != 1:
        raise ValueError('Local host mapping cannot be identified uniquely')
    host = hosts[0]
    mapped = state['app-server-project-id-by-legacy-project-id-by-host'][host]
    if not isinstance(mapped, dict):
        raise ValueError('Unsupported host mapping')
    registered_roots = {normalize(r) for p in existing.values() for r in p['rootPaths']}
    result = []
    for p in projects:
        paths = [r['path'] for r in roots if r['project_id'] == p['id']]
        keys = sorted({r['key'] for r in aliases if r['project_id'] == p['id']})
        reasons = []
        if len(keys) != 1:
            reasons.append('ambiguous_or_missing_legacy_id')
        key = keys[0] if len(keys) == 1 else None
        if key in existing:
            reasons.append('already_registered')
        if key is not None and key in mapped and mapped[key] != p['id']:
            reasons.append('conflicting_mapping')
        if not paths:
            reasons.append('no_roots')
        if any(not Path(r).is_dir() for r in paths):
            reasons.append('missing_directory')
        if any(not ntpath.isabs(r) for r in paths):
            reasons.append('relative_directory')
        if any(path_kind(r) == 'worktree' for r in paths):
            reasons.append('worktree')
        if (key or '').startswith('g-p-') or any(path_kind(r) == 'cloud_linked' for r in paths):
            reasons.append('cloud_linked_needs_separate_review')
        if any(normalize(r) in registered_roots for r in paths) and key not in existing:
            reasons.append('overlaps_registered_project')
        # Existing multi-root projects can contain empty auxiliary directories.
        if paths and all(Path(r).is_dir() and not any(Path(r).iterdir()) for r in paths):
            reasons.append('all_roots_empty')
        record = dict(id=key, name=p['name'], rootPaths=paths,
                      createdAt=p['created_at_ms'], updatedAt=p['updated_at_ms'])
        result.append(dict(backend_id=p['id'], legacy_id=key, record=record,
                           eligible=not reasons, reasons=reasons, evidence_sha256=digest([p, paths, keys])))
    deleted = [a for a in aliases if not any(p['id'] == a['project_id'] for p in projects)]
    return host, result, deleted


def session_paths(home):
    counts, errors = {}, []
    keys = {'cwd','repository_root','repositoryroot','repo_root','reporoot','worktree_path','worktreepath','rootpaths','workspace_root'}
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in keys:
                    for p in child if isinstance(child, list) else [child]:
                        if isinstance(p, str) and ntpath.isabs(p):
                            norm = normalize(p)
                            counts[norm] = counts.get(norm, 0) + 1
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for folder in ['sessions', 'archived_sessions']:
        for file in (home/folder).rglob('*.jsonl'):
            with file.open(encoding='utf-8') as stream:
                for number, line in enumerate(stream, 1):
                    if not any(k in line[:256] for k in ['"session_meta"','"turn_context"']):
                        continue
                    try:
                        row = json.loads(line)
                        if row.get('type') in {'session_meta','turn_context'}:
                            walk(row.get('payload', {}))
                    except ValueError:
                        errors.append({'file': str(file), 'line': number})
    return {'path_counts': counts, 'parse_errors': errors}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def safe_output(path, home):
    if path.resolve().is_relative_to(home.resolve()):
        raise ValueError('Reports and plans must be outside CODEX_HOME')


def build_plan(home, selected):
    _, state = load_state(home)
    host, rows, _ = candidates(home, state)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError('Select one or more distinct legacy IDs')
    chosen = []
    paths = set()
    for key in selected:
        matches = [r for r in rows if r['legacy_id'] == key]
        if len(matches) != 1 or not matches[0]['eligible']:
            raise ValueError('Not an eligible unique candidate: ' + key)
        row = matches[0]
        root_set = {normalize(p) for p in row['record']['rootPaths']}
        if paths & root_set:
            raise ValueError('Selected projects share roots; select only one identity')
        paths |= root_set
        chosen.append(row)
    return {'version': VERSION, 'home': str(home), 'host': host, 'selected': chosen,
            'warning': 'Private local plan. Do not upload to GitHub. Does not recover cloud UI or task ownership.'}


def validate_plan(plan):
    if plan.get('version') != VERSION:
        raise ValueError('Unsupported plan version')
    home = Path(plan['home']).resolve()
    fresh = build_plan(home, [r['legacy_id'] for r in plan['selected']])
    if fresh != plan:
        raise ValueError('Plan or underlying records changed; audit and regenerate the plan')
    return home


def transform(state, plan):
    result = copy.deepcopy(state)
    for row in plan['selected']:
        key = row['legacy_id']
        if key in result['local-projects']:
            raise ValueError('Project already registered; do not reapply')
        result['local-projects'][key] = row['record']
        mapping = result['app-server-project-id-by-legacy-project-id-by-host'][plan['host']]
        if key in mapping and mapping[key] != row['backend_id']:
            raise ValueError('Conflicting mapping')
        mapping[key] = row['backend_id']
        if key not in result['project-order']:
            result['project-order'].append(key)
    for key in set(state) | set(result):
        if key not in FIELDS and state.get(key) != result.get(key):
            raise ValueError('Unexpected unrelated change')
    return result


def require_closed():
    if os.name != 'nt':
        raise ValueError('Applying repairs is supported on Windows only')
    ps = "@(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.Name -ieq 'codex.exe' -or $_.Name -ieq 'ChatGPT.exe' } | Select-Object -ExpandProperty ProcessId) | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',ps], check=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if json.loads(result.stdout or '[]'):
        raise ValueError('Close Codex/ChatGPT and Codex CLI processes before applying; no processes were terminated')


def apply_plan(plan, backup_root):
    require_closed()
    home = validate_plan(plan)
    safe_output(backup_root, home)
    target = home/'.codex-global-state.json'
    raw, before = load_state(home)
    after = transform(before, plan)
    backup = backup_root / dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True, exist_ok=False)
    with (backup/target.name).open('xb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    if (backup/target.name).read_bytes() != raw:
        raise ValueError('Backup verification failed')
    with closing(sqlite3.connect((home/'state_5.sqlite').as_uri()+'?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(backup/'state_5.sqlite')) as dst:
            src.backup(dst)
    require_closed()
    validate_plan(plan)
    if target.read_bytes() != raw:
        raise ValueError('State changed during backup; no state write performed')
    atomic_json(target, after)
    verified = json.loads(target.read_bytes())
    if verified != after:
        raise ValueError('Readback differs; inspect backup before continuing')
    result = {'status':'SUCCESS', 'backup':str(backup), 'added':len(plan['selected']),
              'count_before':len(before['local-projects']), 'count_after':len(after['local-projects']),
              'before_sha256':hashlib.sha256(raw).hexdigest(), 'after_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
              'database_writes':False, 'thread_assignment_changes':False, 'ui_check':'Required after restart'}
    atomic_json(backup/'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))))
    sub = parser.add_subparsers(dest='command', required=True)
    audit = sub.add_parser('audit')
    audit.add_argument('--out', type=Path, required=True)
    audit.add_argument('--sessions', action='store_true')
    plan = sub.add_parser('plan')
    plan.add_argument('--select', action='append', required=True, help='Verified legacy ID from audit; repeat for multiple projects')
    plan.add_argument('--out', type=Path, required=True)
    check = sub.add_parser('check')
    check.add_argument('plan', type=Path)
    apply = sub.add_parser('apply')
    apply.add_argument('plan', type=Path)
    apply.add_argument('--confirm', action='store_true', required=True)
    apply.add_argument('--backup-dir', type=Path, default=Path('.local/backups'))
    args = parser.parse_args()
    home = args.home.resolve()
    if args.command == 'audit':
        safe_output(args.out, home)
        _, state = load_state(home)
        host, rows, deleted = candidates(home, state)
        report = {'format':'JSON desktop registration + SQLite backend', 'home':str(home), 'host':host,
                  'current_projects':state['local-projects'], 'candidates':rows, 'deleted_backend_aliases':deleted}
        if args.sessions:
            report['sessions'] = session_paths(home)
        atomic_json(args.out, report)
        print('Audit saved locally. Review eligible candidates; no client state changed.')
    elif args.command == 'plan':
        safe_output(args.out, home)
        atomic_json(args.out, build_plan(home, args.select))
        print('Dry-run plan saved. Review the exact records before applying.')
    else:
        data = json.loads(args.plan.read_text(encoding='utf-8'))
        if args.command == 'check':
            validate_plan(data)
            print('PASS: plan still matches live records; no state changed')
        else:
            print(json.dumps(apply_plan(data, args.backup_dir.resolve()), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('FAILED:', error, file=sys.stderr)
        sys.exit(1)
