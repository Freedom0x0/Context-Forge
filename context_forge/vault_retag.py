"""Retag Gen1 vault documents for the boundary-trio model.

Adds `confidence` / `role` / `technologies` / `domains` and the explicit
access keys, and moves rules whose declared `paths` are evidence provenance
rather than applicability into `evidence_paths` (provenance is never dropped).
Never deletes a document and never
clears `project` (it becomes provenance). Dry-run is the default; changed
files are copied to a backup directory first so the run is reversible.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .index import iter_vault_documents, parse_frontmatter, yaml_scalar
from .memory_models import DEFAULT_OWNER


_VERIFICATION = ('typecheck', 'tsc', 'lint', 'eslint', 'prettier', 'pytest', 'jest', 'vitest',
                 'test', 'diff', 'output', 'result', 'probe', 'command', 'run', 'script',
                 'check', 'verify', 'baseline', 'count', 'listing', 'bash', 'curl', 'fetch')
_OBSERVATION = ('read', 'edit', 'grep', 'write', 'inspect', 'look', 'refs', 'schema', 'chain',
                'snippet', 'signature', 'inspection')
_NARRATION = ('assistant', 'user', 'model', 'skill', 'session', 'transcript', 'summary',
              'analysis', 'explanation', 'message', 'final', 'reason', 'request', 'wording',
              'note', 'plan', 'decision', 'question')

_PREFERENCE = ('不要', '别写', '别加', '不用加', '避免', '无需', '不必', '禁止', '优先', '建议',
               '习惯', '风格', '命名', '注释', '宁可', '尽量不要')
_PRESCRIPTIVE = ('必须', '需要', '应', '先', '再', '然后', '步骤', '使用', '设置', '配置',
                 '检查', '验证', '改为', '切换', '调用', '新增', '删除', '迁移', '拆分', '收敛',
                 '确保', '记住', '要做', '要先')
_GENERALISES = ('一般', '通常', '所有', '凡是', '任何', '总是', '每次', '统一', '通用',
                '如果', '无论', '凡是', '都会', '都会')

_TECHNOLOGIES = ('antd', 'umi', 'react', 'typescript', 'javascript', 'sqlite', 'postgres',
                 'mysql', 'redis', 'cas', 'oauth', 'zod', 'pytest', 'python', 'node', 'fastapi',
                 'pydantic', 'vite', 'webpack', 'httpx', 'deepseek', 'ollama', 'claude', 'mcp',
                 'typer', 'vue', 'graphql', 'docker', 'git')

_DOMAINS = {
    'auth': ('auth', 'login', 'cas', 'sso', 'oauth', 'jwt', 'permission', '鉴权', '登录'),
    'proxy': ('proxy', 'gateway', 'upstream', '代理', '转发'),
    'search': ('search', 'pagination', '搜索', '筛选', '分页'),
    'table': ('table', 'grid', '表格', '列表页'),
    'form': ('validation', 'zod', 'dto', '表单', '校验'),
    'api': ('graphql', 'httpx', '接口', '请求体'),
    'state': ('redux', 'zustand', 'pinia', '状态管理'),
    'routing': ('router', 'navigation', '路由'),
    'storage': ('sqlite', 'postgres', 'mysql', 'migration', 'orm', '事务'),
    'testing': ('pytest', 'jest', 'vitest', 'coverage', '测试'),
    'build': ('webpack', 'vite', 'rollup', 'esbuild', 'babel', '构建'),
    'cli': ('argparse', 'cobra', 'typer', 'click', '命令行'),
    'convention': ('convention', 'style guide', 'naming', '注释', '命名', '约定', '风格'),
    'memory': ('memory', 'recall', 'vault', '记忆', '召回', '复盘', '沉淀'),
}

_SECRET = re.compile(
    r'(-----BEGIN [A-Z ]*PRIVATE KEY-----)|(sk-[A-Za-z0-9]{16,})|(ghp_[A-Za-z0-9]{20,})|'
    r'(AKIA[0-9A-Z]{16})|((?:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*["\']?[^\s,;\'"]{8,})',
    re.I)
_CONCRETE_FILE = re.compile(
    r'([\w.-]+/[\w.*-]+)'
    r'|(\b[\w-]+\.(?:ts|tsx|js|jsx|py|md|json|toml|ya?ml|css|go|rs)\b)'
    r'|(\.\w{1,4}:\d+)', re.I)
_NUMBERED = re.compile(r'^[eE][vV]?\d+[-\w]*$')


def _hit(text: str, word: str) -> bool:
    if word.isascii():
        return re.search(r'(?<![\w])' + re.escape(word) + r'(?![\w])', text, re.I) is not None
    return word in text


def _classify_source(identity: str) -> str:
    text = identity.strip().lower()
    if ':' in text and text.split(':', 1)[0] in {'tool-result', 'tool', 'tool_result', 'bash',
                                                'read', 'edit', 'grep', 'write', 'typecheck', 'lint'}:
        return 'observation'
    if any(_hit(text, word) for word in _VERIFICATION):
        return 'verification'
    if any(_hit(text, word) for word in _NARRATION):
        return 'narration'
    if _NUMBERED.match(identity.strip()) or _CONCRETE_FILE.search(identity):
        return 'observation'
    if any(_hit(text, word) for word in _OBSERVATION):
        return 'observation'
    return 'narration'


def evidence_strength(sources: tuple[str, ...], declared: str | None = None) -> tuple[str, str]:
    """Evidence-source strength, optionally capped by the declared confidence.

    A derived rule must never read as more certain than the knowledge it
    was distilled from (P1.0b: no child-over-parent inversion).
    """
    kinds = {_classify_source(source) for source in sources}
    if not sources:
        strength, basis = 'low', 'no retained sources'
    elif 'verification' in kinds and len(set(sources)) >= 2:
        strength, basis = 'high', f'{len(set(sources))} sources incl. verification artifact'
    elif kinds - {'narration'}:
        strength, basis = 'medium', f'sources classified {sorted(kinds)}'
    else:
        strength, basis = 'low', 'narration-only sources'
    rank = {'low': 1, 'medium': 2, 'high': 3}
    declared_rank = rank.get((declared or '').strip().lower())
    if declared_rank and declared_rank < rank[strength]:
        return (declared or '').strip().lower(), f'{basis}; capped by declared {declared}'
    return strength, basis


def names_declared_paths(instruction: str, patterns: tuple[str, ...]) -> bool:
    text = instruction.lower()
    for pattern in patterns:
        if pattern.lower() in text:
            return True
        for segment in re.split(r'[/*]', pattern):
            segment = segment.strip().lower().strip('.')
            if len(segment) > 3 and segment in text:
                return True
    return False


def classify_role(kind: str, instruction: str, body: str) -> tuple[str, str]:
    text = instruction if kind == 'rule' else body
    prescriptive = any(_hit(text, word) for word in _PRESCRIPTIVE)
    preferential = any(_hit(text, word) for word in _PREFERENCE)
    if preferential and not prescriptive:
        return 'preference', 'states how the work should be done'
    if prescriptive:
        return 'procedure', 'states an action sequence or a required step'
    if kind != 'rule' and _CONCRETE_FILE.search(text) and not any(_hit(text, word) for word in _GENERALISES):
        return 'observation', 'located project fact: names concrete files without a generalisation'
    if kind == 'rule':
        return 'claim', 'rules carry semantic or procedural knowledge, never a located episode'
    return 'claim', 'states what is generally true'


def detect_tags(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    technologies = tuple(sorted({token for token in _TECHNOLOGIES if _hit(text, token)}))
    domains = tuple(sorted({domain for domain, words in _DOMAINS.items()
                            if any(_hit(text, word) for word in words)}))
    return technologies, domains


def detect_sensitive(text: str) -> tuple[str | None, str]:
    """Only secrets still control visibility; project-level access was removed.

    An internal / loopback URL used to be tagged `selected_projects` + `allowed_projects`.
    Cross-project recall is the product, so that tag no longer exists; the URL text is left
    untouched and the owner boundary is the only gate.
    """
    if _SECRET.search(text):
        return 'private', 'credential-shaped content detected'
    return None, ''


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in '"\'':
        try:
            return json.loads(text) if text[0] == '"' else text[1:-1]
        except ValueError:
            return text[1:-1]
    return text


def _paths(front: dict) -> tuple[str, ...]:
    return _list(front, 'paths')


def _list(front: dict, key: str) -> tuple[str, ...]:
    raw = front.get(key)
    if raw is None:
        return ()
    values = raw.split(',') if isinstance(raw, str) else (raw if isinstance(raw, list) else [raw])
    return tuple(text for text in (_unquote(str(value)) for value in values) if text)


def _needs_quoting(raw: str) -> bool:
    value = raw.strip()
    if not value or value[0] in '"\'':
        return False
    return yaml_scalar(value) != value


def _render(front_block: str, updates: dict[str, object],
            removals: tuple[str, ...] = ()) -> tuple[str, tuple[str, ...]]:
    rendered = []
    seen = set()
    quoted = []
    for line in front_block.splitlines():
        match = re.match(r'^([A-Za-z_][\w-]*)\s*:(.*)$', line)
        key = match.group(1) if match else None
        if key and key in updates:
            seen.add(key)
            rendered.append(f'{key}: {yaml_scalar(updates[key])}')
        elif key and key in removals:
            seen.add(key)
        elif key and _needs_quoting(match.group(2)):
            quoted.append(key)
            rendered.append(f'{key}: {yaml_scalar(_unquote(match.group(2)))}')
        else:
            rendered.append(line)
    for key, value in updates.items():
        if key not in seen:
            rendered.append(f'{key}: {yaml_scalar(value)}')
    return '\n'.join(rendered), tuple(quoted)


def _split(text: str) -> tuple[bool, str, str]:
    if not text.startswith('---\n'):
        return False, '', text
    marker = text.find('\n---', 4)
    if marker < 0:
        return False, '', text
    return True, text[4:marker], text[marker + 4:]


def plan(root: Path) -> dict:
    """Compute every retag decision without touching the filesystem."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    report = {'owner': DEFAULT_OWNER, 'documents': 0, 'changed': 0, 'unchanged': 0,
              'paths_collapsed': 0, 'skipped_user_owned': 0, 'quoted_scalars': 0,
              'diagnostics': [], 'items': []}
    targets = list(iter_vault_documents(root))
    report['documents'] = len(targets)
    parsed = {}
    for path in targets:
        text = path.read_text(encoding='utf-8')
        front, body = parse_frontmatter(text)
        parsed[path] = (text, front, body)
    knowledge = {str(front.get('id') or path.stem): (front, body)
                 for path, (_, front, body) in parsed.items()
                 if path.relative_to(root).as_posix().startswith('knowledge/')}
    for path in targets:
        relative = path.relative_to(root).as_posix()
        text, front, body = parsed[path]
        kind = 'rule' if relative.startswith('rules/') else (
            'knowledge' if relative.startswith('knowledge/') else 'review')
        instruction = body.strip()
        project = str(front.get('project') or '').strip()
        legacy_id = str(front.get('id') or path.stem)
        if str(front.get('user_owned', '')).strip().lower() == 'true':
            report['skipped_user_owned'] += 1
            report['diagnostics'].append(f'{relative}: user_owned; left for a human correction')
            continue
        parent_front, parent_body = knowledge.get(str(front.get('source_knowledge_id') or ''), ({}, ''))
        sources = _list(parent_front, 'sources') or _list(front, 'sources')
        updates: dict[str, object] = {}
        removals: tuple[str, ...] = ()
        reasons = []
        if front.get('confidence') in (None, ''):
            strength, basis = evidence_strength(
                sources, str(parent_front.get('confidence')) if parent_front else None)
            updates['confidence'] = strength
            reasons.append(f'confidence={strength} ({basis})')
        role, role_basis = classify_role(kind, instruction, body or parent_body)
        if str(front.get('role') or '') != role:
            updates['role'] = role
            reasons.append(f'role={role} ({role_basis})')
        technologies, domains = detect_tags(f'{instruction}\n{body}\n{parent_body}')
        if technologies and not _list(front, 'technologies'):
            updates['technologies'] = ','.join(technologies)
            reasons.append(f'technologies={",".join(technologies)}')
        if domains and not _list(front, 'domains'):
            updates['domains'] = ','.join(domains)
            reasons.append(f'domains={",".join(domains)}')
        privacy, sensitive_basis = detect_sensitive(f'{instruction}\n{body}')
        if privacy and str(front.get('privacy', '')) != privacy:
            updates['privacy'] = privacy
            reasons.append(f'privacy={privacy} ({sensitive_basis})')
        patterns = _paths(front)
        if kind == 'rule' and patterns and not names_declared_paths(instruction, patterns):
            removals = ('paths',)
            retained = tuple(dict.fromkeys(_list(front, 'evidence_paths') + patterns))
            updates['evidence_paths'] = ','.join(retained)
            report['paths_collapsed'] += 1
            reasons.append('paths moved to evidence_paths: declared globs are evidence provenance, '
                           'not applicability; provenance is never dropped')
        present, front_block, _rest = _split(text)
        unsafe = tuple(match.group(1) for match in
                       (re.match(r'^([A-Za-z_][\w-]*)\s*:(.*)$', line) for line in front_block.splitlines())
                       if match and _needs_quoting(match.group(2))) if present else ()
        if unsafe:
            report['quoted_scalars'] += len(unsafe)
            reasons.append(f'quoted unsafe YAML scalars: {",".join(unsafe)}')
        if not updates and not removals and not unsafe:
            report['unchanged'] += 1
            continue
        report['changed'] += 1
        report['items'].append({'path': relative, 'project': project or None,
                                'before': {key: front.get(key) for key in (*updates, *removals)},
                                'after': {**updates, **{key: None for key in removals}},
                                'reasons': reasons,
                                'content_hash': hashlib.sha256(text.encode('utf-8')).hexdigest()})
    return report


def retag(root: Path, *, dry_run: bool = True) -> dict:
    root = Path(root).resolve()
    report = plan(root)
    report['dry_run'] = dry_run
    report['backup'] = None
    if dry_run:
        return report
    backup = root / '.retag-backup'
    backup.mkdir(exist_ok=True)
    applied = 0
    for item in report['items']:
        path = root / item['path']
        text = path.read_text(encoding='utf-8')
        present, front_block, rest = _split(text)
        if not present:
            report['diagnostics'].append(f"{item['path']}: no frontmatter block")
            continue
        updates = {key: value for key, value in item['after'].items() if value is not None}
        removals = tuple(key for key, value in item['after'].items() if value is None)
        target = backup / item['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(path, target)
        rendered, quoted = _render(front_block, updates, removals)
        item['quoted'] = list(quoted)
        path.write_text(f'---\n{rendered}\n---\n{rest}',
                        encoding='utf-8', newline='\n')
        applied += 1
    report['backup'] = str(backup)
    report['applied'] = applied
    return report


def rollback(root: Path, *, backup: Path | None = None) -> int:
    root = Path(root).resolve()
    source = Path(backup) if backup else root / '.retag-backup'
    if not source.is_dir():
        raise FileNotFoundError(source)
    restored = 0
    for path in sorted(source.rglob('*.md')):
        shutil.copy2(path, root / path.relative_to(source))
        restored += 1
    return restored
