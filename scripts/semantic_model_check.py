"""Compare candidate embedding models on Chinese memory text before indexing anything.

Reads `memory.db` read-only and writes nothing: every vector here is in-memory and thrown
away. For each model it reports four numbers that decide whether the semantic channel is worth
turning on at all — cold-start cost, per-record cost, how much of Chinese survives the
tokenizer, and whether a genuine paraphrase outscores unrelated text on your own corpus.

The last one is the gate. A model can look fine on a public benchmark and still put
"改自己角色要重拉权限判据" above "线上库改坏了要退回上一版" for a rollback query; when that
happens, a higher score threshold cannot save it, because the true paraphrase is the *lower*
number. Relevance of the printed top hits is for a human to judge.

    python scripts/semantic_model_check.py                       # the default shortlist
    python scripts/semantic_model_check.py --models intfloat/multilingual-e5-small
    python scripts/semantic_model_check.py --no-whitelist        # fetch whole repos
    python scripts/semantic_model_check.py --skip-recall         # cost + discrimination only
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

os.environ.setdefault('HF_HUB_DISABLE_PROGRESS_BARS', '1')

DEFAULT_MODELS = (
    'sentence-transformers/all-MiniLM-L6-v2',        # what is configured today
    'paraphrase-multilingual-MiniLM-L12-v2',
    'BAAI/bge-small-zh-v1.5',
    'intfloat/multilingual-e5-small',
)
# Pairs labelled by hand.  `same` must beat every `other` for the model to be usable: the
# margin below is min(same) - max(other), and a negative margin means no threshold works.
PAIRS = (
    ('same', '数据库迁移失败怎么回滚', '线上库改坏了要退回上一版'),
    ('same', '数据库迁移失败怎么回滚', '回滚数据库迁移前必须先备份'),
    ('same', '禁止修改自己的账号类型', '编辑当前登录用户时把角色字段禁用掉'),
    ('other', '数据库迁移失败怎么回滚', '当管理后台的写操作目标是当前登录用户自己时须重拉权限判据'),
    ('other', '数据库迁移失败怎么回滚', '今天天气适合出去跑步'),
    ('other', '禁止修改自己的账号类型', '数据库连接池超时导致请求堆积'),
)
PROBES = (
    '数据库迁移失败怎么回滚',
    '线上库改坏了要退回上一版',
    '账号权限改动后前端仍然显示旧角色',
)
WHITELIST = ('*.json', '*.txt', '*.md', '*.safetensors', '*.model', '*.vocab',
             '*/*.json', '*/*.safetensors', '*/*.model')
MAX_LENGTH = 250          # the retrieval path never sends more than this either


def memory_texts(limit: int | None):
    """(id, project, kind, sanitized-ish text) for every current version, read-only."""
    from context_forge.memory_models import MemoryDraft
    from context_forge.transcript import sanitize_text

    db = Path.home() / '.context-forge' / 'memory.db'
    if not db.exists():
        return []
    connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        'SELECT m.id, v.snapshot_json FROM memories m '
        'JOIN memory_versions v ON v.memory_id = m.id AND v.version = m.current_version '
        'WHERE m.status = ? ORDER BY m.id', ('active',)).fetchall()
    connection.close()
    out = []
    for row in rows:
        draft = MemoryDraft.model_validate_json(row['snapshot_json'])
        projects = draft.provenance.projects or ('(none)',)
        out.append((row['id'], projects[0], draft.kind,
                    sanitize_text(draft.summary + '\n' + draft.body)[:MAX_LENGTH]))
    return out[:limit] if limit else out


def tokenizer_stress(model, samples):
    """How much of a Chinese string survives the model's vocabulary."""
    try:
        tokenizer = model.tokenizer
    except AttributeError:
        return None
    unk_token = getattr(tokenizer, 'unk_token', None)
    if unk_token is None:
        return None
    rates = []
    for text in samples:
        pieces = tokenizer.convert_ids_to_tokens(
            tokenizer(text, return_tensors=None)['input_ids'])
        if pieces:
            rates.append(sum(1 for piece in pieces if piece == unk_token) / len(pieces))
    return max(rates) if rates else None


def load(model_id: str, *, whitelist: bool):
    if whitelist:
        from huggingface_hub import snapshot_download

        snapshot_download(model_id, allow_patterns=list(WHITELIST))
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_id)


def report(model_id: str, records, *, whitelist: bool, skip_recall: bool):
    print(f'\n{"=" * 78}\n{model_id}\n{"=" * 78}')
    started = time.time()
    try:
        model = load(model_id, whitelist=whitelist)
    except Exception as error:
        print(f'  载入失败: {type(error).__name__}: {str(error)[:200]}')
        return None
    load_seconds = time.time() - started

    vectors = []
    encode_start = time.time()
    for record in records:
        vectors.append(model.encode(record[3], normalize_embeddings=True))
    encode_seconds = time.time() - encode_start
    per_record = encode_seconds / len(records) if records else 0.0
    dims = len(vectors[0]) if vectors else 0
    unk = tokenizer_stress(model, [probe for probe in PROBES[:2]])

    def cos(a, b):
        import numpy

        left, right = numpy.asarray(a, dtype=float), numpy.asarray(b, dtype=float)
        denom = float(numpy.linalg.norm(left) * numpy.linalg.norm(right)) or 1.0
        return float(left @ right / denom)

    same, other = [], []
    for label, left, right in PAIRS:
        score = cos(model.encode(left, normalize_embeddings=True),
                    model.encode(right, normalize_embeddings=True))
        (same if label == 'same' else other).append(score)
        print(f'  {score:.3f}  {label:<5} {left[:16]!r} vs {right[:26]!r}')
    margin = min(same) - max(other) if same and other else float('nan')

    print(f'  --- {dims} 维 | 冷启动 {load_seconds:.1f}s | {per_record * 1000:.0f} ms/条 '
          f'({len(records)} 条 = {encode_seconds:.1f}s)')
    if unk is None:
        print('  [UNK] 率: 模型没有 unk_token，跳过')
    else:
        print(f'  中文 [UNK] 率: {unk * 100:.0f}%')
    print(f'  判别裕度 min(真同义) - max(无关) = {margin:+.3f}  '
          f'{"可用" if margin > 0 else "不可用：阈值救不了"}')

    if not skip_recall:
        for probe in PROBES:
            query = model.encode(probe, normalize_embeddings=True)
            scores = sorted(((cos(query, vector), record) for vector, record in zip(vectors, records)),
                            key=lambda item: (-item[0], item[1][0]))
            print(f'\n  查询 {probe!r} 前 5 条:')
            for score, (memory_id, project, kind, text) in scores[:5]:
                print(f'    {score:.3f}  {memory_id[:9]:<10} {project:<18} {kind:<9} '
                      f'{text[:40]!r}')
    return {'model': model_id, 'dims': dims, 'load': round(load_seconds, 1),
            'per_record_ms': round(per_record * 1000), 'unk': unk, 'margin': round(margin, 3)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--models', nargs='*', default=list(DEFAULT_MODELS))
    parser.add_argument('--limit', type=int, default=None, help='只取前 N 条记忆')
    parser.add_argument('--no-whitelist', action='store_true',
                        help='拉完整仓库（默认只拉 json/txt/safetensors/sentencepiece）')
    parser.add_argument('--skip-recall', action='store_true')
    args = parser.parse_args(argv)

    if 'sentence-transformers' not in sys.modules:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            print('需要 `python -m pip install -e ".[semantic]"`')
            return 1

    records = memory_texts(args.limit)
    if not records:
        print('memory.db 里没有 active 记录，只跑判别对')
    print(f'语料: {len(records)} 条 active 记忆，冷启动逐模型计时（模型载入后不卸载）')

    summary = []
    for model_id in args.models:
        entry = report(model_id, records, whitelist=not args.no_whitelist,
                       skip_recall=args.skip_recall)
        if entry:
            summary.append(entry)

    print(f'\n{"=" * 78}\n汇总\n{"=" * 78}')
    print(f'{"模型":<44} {"维":>4} {"冷启动":>7} {"ms/条":>6} {"UNK":>5} {"裕度":>7}  判定')
    for entry in summary:
        unk = '-' if entry['unk'] is None else f"{entry['unk'] * 100:.0f}%"
        print(f'{entry["model"]:<44} {entry["dims"]:>4} {entry["load"]:>6.1f}s '
              f'{entry["per_record_ms"]:>6} {unk:>5} {entry["margin"]:>+7.3f}  '
              f'{"可用" if entry["margin"] > 0 else "不可用"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
