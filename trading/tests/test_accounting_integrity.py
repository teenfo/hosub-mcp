from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from app import journal, settings
from app.trade import ledger, fills

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, 'DB_PATH', tmp_path/'trading.db')
    monkeypatch.setattr(journal, 'DB_PATH', tmp_path/'trading.db')
    return tmp_path

def row(pid):
    with ledger._conn() as c:
        return dict(c.execute('select * from positions where id=?',(pid,)).fetchone())

def test_residual_fee_conservation_and_idempotence(db):
    ledger.open_position(dict(id='p',symbol='123456',rule='orb',side='long',qty=10,entry=100,stop=98,target=104),fill=100,ord_no='B')
    ledger.close_position('p',110,'stop',ord_no='S')
    ledger.reconcile_executions([dict(ord_no='B',code='123456',side='buy',qty=10,price=100,commission=10,tax=0),dict(ord_no='S',code='123456',side='sell',qty=4,price=110,commission=4,tax=1)])
    fills.adopt_residuals({'123456':6}, datetime.now(ZoneInfo('Asia/Seoul')),{'123456':dict(avg_price=100)},set())
    residual=[p for p in ledger.positions('open') if p['rule']=='residual'][0]
    assert residual['parent_position_id']=='p'
    assert residual['entry_fee_krw']==6
    ledger.close_position(residual['id'],110,'residual',ord_no='R')
    ledger.reconcile_executions([dict(ord_no='R',code='123456',side='sell',qty=6,price=110,commission=6,tax=2)])
    ledger.apply_actual_costs();ledger.apply_actual_costs()
    assert row('p')['pnl_krw']+row(residual['id'])['pnl_krw']==77

def test_unknown_cost_is_not_zero():
    c=ledger.cost_breakdown(dict(symbol='123456',qty=10,entry=100,exit=101,side='long',fill_confirmed=1,exit_fill_confirmed=1,exit_fee_krw=3,tax_krw=1))
    assert c['gross_pnl_krw']==10
    assert c['actual_net_pnl_krw'] is None
    assert c['cost_status']=='unverified'

def test_actual_cost_does_not_deduct_slippage_again():
    c=ledger.cost_breakdown(dict(symbol='123456',qty=10,entry=100,exit=110,side='long',fill_confirmed=1,exit_fill_confirmed=1,entry_fee_krw=10,exit_fee_krw=11,tax_krw=2))
    assert c['actual_net_pnl_krw']==77

def test_cumulative_includes_broker_only_days():
    rows=[dict(date='2026-11-05',trades=1,win_rate=100,pnl_krw=10,expectancy_pct=1),dict(date='2026-11-06',trades=0,win_rate=0,pnl_krw=-30,expectancy_pct=0)]
    assert journal.cumulative(rows)['pnl_krw']==-20
    assert journal.cumulative(rows[1:])['pnl_krw']==-30

def test_old_day_not_truncated(db):
    with ledger._conn() as c:
        c.executemany("insert into positions(id,opened,closed,status,qty,entry,exit) values(?,?,?,'closed',1,100,101)",[(str(i),'2026-11-06','2026-11-06') for i in range(501)]+[('old','2026-07-01','2026-07-01')])
    assert len(ledger.positions_closed_on('2026-07-01'))==1

@pytest.mark.asyncio
async def test_historical_reconciliation_uses_requested_day(db, monkeypatch):
    from app.kiwoom.client import client
    async def forbidden():
        raise AssertionError("historical journal must not fetch today's executions")
    monkeypatch.setattr(client, 'executions', forbidden)
    fills.store_today([dict(ord_no='old',code='123456',side='buy',qty=1,price=100,commission=1,tax=0)], '2020-01-01')
    calls=[]
    monkeypatch.setattr(ledger,'reconcile_executions',lambda rows,day: calls.append((day,rows[0]['ord_no'])))
    assert await journal._reconcile_first('2020-01-01')
    assert calls==[('2020-01-01','old')]
