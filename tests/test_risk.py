import hashlib
import sqlite3
from pathlib import Path

import pytest

from context_forge.memory import MemoryStore, MemoryStoreError
from context_forge.memory_models import MemoryDraft, Provenance
from context_forge.provider_http import PROMPT_VERSION, _PROMPT
from context_forge.risk import classify_risk, risk_of


def draft(body, **overrides):
    values = {'kind': 'rule', 'body': body, 'status': 'active',
              'provenance': Provenance(projects=('demo',))}
    values.update(overrides)
    return MemoryDraft(**values)


@pytest.mark.parametrize('body', [
    '上线前先 rm -rf dist/',
    '每次发版前必须 DROP TABLE sessions',
    '部署前 truncate table orders',
    '迁移生产库前先回滚，删除所有旧数据',
    'push --force 覆盖远端历史',
    'git reset --hard 回到干净状态',
])
def test_destructive_instructions_are_high(body):
    assert classify_risk(body) == 'high'


@pytest.mark.parametrize('body', [
    '用户偏好中文注释',
    '删除按钮的样式用 secondary tone',      # destructive verb, no bulk/production cue
    '缓存失效逻辑以角色变更为准',
    'SQLite 事务回滚需要测试连接参数',
])
def test_ordinary_instructions_stay_normal(body):
    assert classify_risk(body) == 'normal'


def test_only_rules_are_gated():
    assert risk_of(draft('上线前 rm -rf dist/')) == 'high'
    assert risk_of(draft('上线前 rm -rf dist/', kind='knowledge')) == 'normal'


def test_unexpected_self_report_degrades_instead_of_failing():
    assert draft('普通内容', risk='medium').risk == 'normal'
    assert draft('高危指令 rm -rf /', risk='HIGH').risk == 'normal'


def test_put_derives_risk_into_snapshot_and_column(tmp_path: Path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    record = store.put('rule-1', draft('上线前 rm -rf dist/', risk='normal'),
                       expected_version=0, idempotency_key='put-1', actor='model',
                       reason='episode finalization')

    assert record.draft.risk == 'high'
    with sqlite3.connect(tmp_path / 'memory.db') as connection:
        assert connection.execute(
            "SELECT risk FROM memory_versions WHERE memory_id='rule-1'").fetchone()[0] == 'high'


def test_recall_marks_high_risk_in_the_explanation(tmp_path: Path):
    store = MemoryStore.initialize(tmp_path / 'memory.db')
    store.put('rule-1', draft('上线前 rm -rf dist/'), expected_version=0,
              idempotency_key='put-1', actor='model', reason='episode finalization')
    store.put('rule-2', draft('上线前先跑一次冒烟测试'), expected_version=0,
              idempotency_key='put-2', actor='model', reason='episode finalization')

    scored = {record.id: breakdown for record, breakdown in
              store.search_scored('上线', project='demo')}

    assert '高危指令' in scored['rule-1'].explanation
    assert '高危指令' not in scored['rule-2'].explanation


def build_v4_database(path: Path) -> Path:
    store = MemoryStore.initialize(path)
    store.put('rule-1', draft('上线前 rm -rf dist/'), expected_version=0,
              idempotency_key='put-1', actor='model', reason='episode finalization')
    store.put('rule-2', draft('用户偏好中文注释'), expected_version=0,
              idempotency_key='put-2', actor='model', reason='episode finalization')
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE memory_versions DROP COLUMN risk')
        connection.execute('PRAGMA user_version=4')
    return path


def test_v4_database_gains_a_backfilled_risk_column(tmp_path: Path):
    path = build_v4_database(tmp_path / 'memory.db')

    with pytest.raises(MemoryStoreError, match='initialize/migrate'):
        MemoryStore(path)                       # ordinary open must not migrate

    report = MemoryStore.migrate(path, dry_run=False)

    assert report['to'] == 7
    with sqlite3.connect(path) as connection:
        assert dict(connection.execute(
            'SELECT memory_id, risk FROM memory_versions').fetchall()) == {
                'rule-1': 'high', 'rule-2': 'normal'}
        assert MemoryStore(path).get('rule-1').draft.risk == 'high'


def test_migrate_dry_run_reports_without_writing(tmp_path: Path):
    path = build_v4_database(tmp_path / 'memory.db')
    before = path.read_bytes()

    report = MemoryStore.migrate(path)

    assert report['dry_run'] is True
    assert path.read_bytes() == before         # risk column absent until it is run


def test_prompt_digest_is_pinned_to_the_recorded_version():
    """Editing the prompt without bumping PROMPT_VERSION must fail here."""
    assert hashlib.sha256(_PROMPT.encode('utf-8')).hexdigest() == (
        '37bebd7371b452b3ff792b0ebe9298ffe69b9acd57000f4d3b3e179e07717bde')
    assert PROMPT_VERSION == 'session-artifacts-v1'
