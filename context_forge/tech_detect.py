"""Detect the technology stack and problem domains of a project root.

Used at recall time to supply the retrieval context; memory-side
`ApplicabilityScope` tags are matched against this profile. Signals come
from manifests, lockfiles and import/require scanning only.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TechProfile:
    technologies: frozenset[str]
    domains: frozenset[str]
    raw_evidence: dict[str, tuple[str, ...]]


_ALIASES = {
    'react-dom': 'react', 'react-native': 'react',
    'ant-design': 'antd',
    'umijs': 'umi',
    'sqlite3': 'sqlite', 'aiosqlite': 'sqlite', 'pysqlite3': 'sqlite',
    'python-frontmatter': 'frontmatter',
}

_DOMAIN_KEYWORDS = {
    'auth': ('auth', 'login', 'logout', 'cas', 'sso', 'oauth', 'jwt', 'session', 'permission', 'role'),
    'proxy': ('proxy', 'gateway', 'upstream', 'rewrite'),
    'search': ('search', 'filter', 'query', 'fts', 'elastic'),
    'table': ('table', 'grid', 'datagrid', 'pagination'),
    'form': ('form', 'input', 'validation', 'schema', 'zod', 'dto'),
    'api': ('api', 'request', 'client', 'rest', 'graphql', 'httpx', 'axios'),
    'state': ('store', 'state', 'redux', 'zustand', 'pinia'),
    'routing': ('router', 'route', 'navigation'),
    'storage': ('db', 'database', 'sql', 'migration', 'orm', 'sqlite', 'postgres', 'redis'),
    'testing': ('test', 'pytest', 'jest', 'vitest', 'coverage', 'mock'),
    'build': ('build', 'bundler', 'webpack', 'vite', 'rollup', 'esbuild', 'babel'),
    'cli': ('cli', 'command', 'typer', 'click', 'argparse', 'cobra'),
}

_SOURCE_SUFFIXES = {'.py', '.ts', '.tsx', '.js', '.jsx', '.go', '.rs', '.java'}
_SKIP_DIRS = {'node_modules', '.git', '.venv', 'venv', '__pycache__', 'dist', 'build', '.pytest_cache'}
_IMPORT_RE = re.compile(r'^\s*(?:import\s+([\w@][\w@/\-\.]*)|from\s+([\w@][\w@/\-\.]*)\s+import|'
                       r'const\s+\w+\s*=\s*require\(\s*[\'"]([\w@][\w@/\-\.]*)[\'"]\s*\))', re.M)
_REQUIRE_RE = re.compile(r'require\(\s*[\'"]([\w@][\w@/\-\.]*)[\'"]\s*\)')
_GO_IMPORT_RE = re.compile(r'^\s*(?:import\s+)?(?:\w+\s+)?"([^"\s]+)"', re.M)
_REQ_NAME = re.compile(r'^[A-Za-z0-9][\w.\-]*$')


def _root_package(name: str) -> str:
    return name.strip().replace('\\', '/').split('/')[0].split('.')[0]


def _normalize(name: str, skip: frozenset[str]) -> str | None:
    cleaned = name.strip().lower()
    if not cleaned or cleaned.startswith('_') or cleaned.startswith('@types/'):
        return None
    cleaned = cleaned.rsplit('/', 1)[-1] if cleaned.startswith('@') else cleaned.split('/')[0]
    token = _ALIASES.get(cleaned, cleaned.replace('_', '-'))
    root = _root_package(token)
    if not root or root.startswith('-') or root in skip:
        return None
    if _root_package(cleaned) in sys.stdlib_module_names and cleaned not in _ALIASES:
        return None
    return root


def _collect(evidence: dict[str, list[str]], name: str, skip: frozenset[str]) -> None:
    token = _normalize(name, skip)
    if token:
        evidence.setdefault(token, []).append(name)


def _pyproject_names(text: str):
    for block in re.finditer(r'=\s*\[(.*?)\]', text, re.S):
        for quoted in re.finditer(r'["\']([^"\']+)["\']', block.group(1)):
            name = re.split(r'[\[<>=!;~\s]', quoted.group(1), maxsplit=1)[0]
            if _REQ_NAME.match(name):
                yield name
    section = ''
    for line in text.splitlines():
        header = re.match(r'\s*\[([^\]]+)\]', line)
        if header:
            section = header.group(1)
        elif section.endswith(('dependencies', 'dev-dependencies')):
            assignment = re.match(r'\s*([\w\.\-]+)\s*=\s*["\']', line)
            if assignment:
                yield assignment.group(1)


def _read_manifests(root: Path, evidence: dict[str, list[str]], skip: frozenset[str]) -> None:
    package = root / 'package.json'
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        for section in ('dependencies', 'devDependencies', 'peerDependencies'):
            for name in data.get(section) or {}:
                _collect(evidence, name, skip)
    for name in ('requirements.txt', 'requirements-dev.txt'):
        requirements = root / name
        if requirements.is_file():
            for line in requirements.read_text(encoding='utf-8', errors='replace').splitlines():
                candidate = re.split(r'[<>=!~;\s\[]', line.strip(), 1)[0]
                if candidate and not candidate.startswith('#'):
                    _collect(evidence, candidate, skip)
    go = root / 'go.mod'
    if go.is_file():
        for line in go.read_text(encoding='utf-8', errors='replace').splitlines():
            match = re.match(r'\s*(?:require\s+)?([\w\.\-/]+)\s+v\d', line)
            if match:
                _collect(evidence, match.group(1), skip)
    pyproject = root / 'pyproject.toml'
    if pyproject.is_file():
        for name in _pyproject_names(pyproject.read_text(encoding='utf-8', errors='replace')):
            _collect(evidence, name, skip)
    for lockfile in ('package-lock.json', 'poetry.lock', 'uv.lock', 'Cargo.lock', 'yarn.lock'):
        locked = root / lockfile
        if locked.is_file():
            text = locked.read_text(encoding='utf-8', errors='replace')
            for match in re.finditer(r'(?:name|package)\s*[=:]\s*["\']?([\w@][\w@/\-\.]*)', text):
                _collect(evidence, match.group(1), skip)


def _read_imports(root: Path, evidence: dict[str, list[str]], skip: frozenset[str]) -> list[str]:
    findings = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS or part.startswith('.') for part in path.parts[:-1]):
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        names = [group for match in _IMPORT_RE.finditer(text) for group in match.groups() if group]
        names += [match.group(1) for match in _REQUIRE_RE.finditer(text)]
        if path.suffix == '.go':
            names += [match.group(1) for match in _GO_IMPORT_RE.finditer(text)]
        for name in names:
            _collect(evidence, name, skip)
        findings.append(path.relative_to(root).as_posix().lower())
    return findings


def detect(root: Path) -> TechProfile:
    root = Path(root).resolve()
    skip = frozenset(variant for path in root.iterdir()
                     if path.name not in _SKIP_DIRS and (path.is_dir() or path.suffix == '.py')
                     for variant in (_root_package(path.name), _root_package(path.name.replace('_', '-'))))
    evidence: dict[str, list[str]] = {}
    _read_manifests(root, evidence, skip)
    paths = _read_imports(root, evidence, skip)
    haystack = ' '.join(list(evidence) + paths)
    domains = {domain: tuple(hits) for domain, hits in _DOMAIN_KEYWORDS.items()
               if any(keyword in haystack for keyword in hits)}
    return TechProfile(
        technologies=frozenset(evidence),
        domains=frozenset(domains),
        raw_evidence={token: tuple(dict.fromkeys(sources)) for token, sources in sorted(evidence.items())},
    )
