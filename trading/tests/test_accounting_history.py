from app.trade.accounting_audit import plan, daily_evidence

def position(pid='p',rule='orb',**kw):
    return dict(dict(id=pid,status='closed',env='mock',symbol='123456',exec_symbol=None,
                rule=rule,side='long',qty=10,entry=100,exit=110,exit_qty=None,
                opened='2026-11-06T09:00:00+09:00',closed='2026-11-06T10:00:00+09:00',
                ord_no='B',exit_ord_no='S',entry_fee_krw=None,exit_fee_krw=None,
                tax_krw=None,fill_confirmed=1,exit_fill_confirmed=0),**kw)

def fill(side,no,**kw):
    return dict(dict(env='mock',d='2026-11-06',code='123456',side=side,ord_no=no,
                     qty=10,price=100 if side=='buy' else 110,commission=10,tax=0),**kw)

def order(pid):
    return dict(created='2026-11-06T01:00:00+00:00',link_pos=pid,result='{"ord_no":"S"}')

def test_exact_order_fees_recover():
    p=position()
    out=plan([p],[fill('buy','B'),fill('sell','S')],[order('p')])
    patch=out['patches'][0]['patch']
    assert patch['entry_fee_krw']==10 and patch['exit_fee_krw']==10
    assert patch.get('exit_qty') is None

def test_cross_environment_is_not_recovered():
    assert not plan([position()],[fill('buy','B',env='real')],[])['patches']

def test_other_day_reused_order_is_not_recovered():
    assert not plan([position()],[fill('buy','B',d='2026-11-05')],[])['patches']

def test_duplicate_residual_only_void_with_authoritative_owner():
    ps=[position(),position('r','residual',ord_no=None,qty=3)]
    patches=plan(ps,[fill('buy','B'),fill('sell','S')],[order('p')])['patches']
    assert next(x['patch'] for x in patches if x['id']=='r')['status']=='void'
    assert not any(x['patch'].get('status')=='void' for x in plan(ps,[fill('sell','S')],[])['patches'])

def test_ambiguous_buy_claim_not_recovered():
    ps=[position(),position('p2')]
    assert not plan(ps,[fill('buy','B')],[])['patches']

def test_existing_complete_exit_is_not_shrunk_by_short_snapshot():
    p=position(entry_fee_krw=10,exit_fee_krw=10,tax_krw=0,exit_fill_confirmed=1)
    assert not plan([p],[fill('sell','S',qty=3)],[order('p')])['patches']

def test_known_partial_exit_can_expand_from_owned_fills():
    p=position(entry_fee_krw=10,exit_fee_krw=3,tax_krw=0,exit_fill_confirmed=1,exit_qty=3)
    patch=plan([p],[fill('sell','S')],[order('p')])['patches'][0]['patch']
    assert patch.get('exit_qty') is None and patch['exit_fee_krw']==10

def test_daily_difference_decomposition_no_fake_allocation():
    p=position(entry_fee_krw=10,exit_fee_krw=10,tax_krw=0,exit_fill_confirmed=1)
    p['costs']=dict(cost_status='actual',gross_pnl_krw=100,commission_krw=20,tax_krw=0)
    broker=dict(env='mock',realized=75,commission=30,tax=0)
    r=daily_evidence('2026-11-06',[p],broker,[fill('buy','B'),fill('sell','S')])
    assert r['gross_basis_difference_krw']==5
    assert r['realized_commission_difference_krw']==10
    assert r['net_difference_krw']==-5
    assert r['detail_status']=='broker_detail_required'

def test_overnight_does_not_claim_gross_basis():
    p=position(opened='2026-11-05T09:00:00+09:00')
    r=daily_evidence('2026-11-06',[p],dict(env='mock',realized=75,commission=30,tax=0),[fill('sell','S')])
    assert 'gross_basis_difference_krw' not in r

def test_mixed_environment_rejected():
    r=daily_evidence('2026-11-06',[position()],dict(env='real',realized=0,commission=0,tax=0),[])
    assert r['status']=='mixed_environment'

def test_decimal_execution_price_is_preserved():
    from app.kiwoom.realized import parse_executions
    rows=parse_executions({"cntr":[{"cntr_qty":"3","cntr_pric":"100.125","ord_no":"B","io_tp_nm":"+매수"}]})
    assert rows[0]["price"]==100.125

def test_weighted_fill_amount_not_rounded_before_pnl(tmp_path, monkeypatch):
    from app.trade import ledger
    monkeypatch.setattr(ledger,"DB_PATH",tmp_path/"trading.db")
    ledger.open_position(dict(id="p",symbol="123456",side="long",rule="orb",qty=3,entry=100,stop=98,target=103),fill=100,ord_no="B")
    ledger.close_position("p",102,"target",ord_no="S")
    rows=[fill("buy","B",qty=2,price=100,commission=0),fill("buy","B",qty=1,price=101,commission=0),
          fill("sell","S",qty=1,price=101,commission=0),fill("sell","S",qty=2,price=102,commission=0)]
    ledger.reconcile_executions(rows)
    p=ledger.positions("closed")[0]
    assert p["pnl_krw"]==4.0
