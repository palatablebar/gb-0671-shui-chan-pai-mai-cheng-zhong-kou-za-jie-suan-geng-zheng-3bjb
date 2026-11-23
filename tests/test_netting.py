from app.engine import evaluate
from app.modules.netting.rules import netting_decision, verify_command, POLICIES


def test_closed_first_nets_clean_tickets(sample):
    netting = sample['netting']
    assert netting['policy'] == 'closed_first'
    assert netting['status'] == '可出指令'
    assert netting['checks']['ok'] is True
    assert netting['summary']['included'] == 19
    assert netting['summary']['held'] == 6
    assert netting['basis']['net_total'] == '18071.91'


def test_entity_nets_receivable_against_boat_charge(sample):
    rows = {row['entity']: row for row in sample['netting']['entities']}
    assert rows['E-101']['receivable'] == '6742.26'
    assert rows['E-101']['payable'] == '1800.00'
    assert rows['E-101']['net'] == '4942.26'
    assert rows['E-101']['direction'] == '划付主体'


def test_late_correction_ticket_is_held_not_guessed(sample):
    held = {row['ticket']: row for row in sample['netting']['held_tickets']}
    assert held['00010']['code'] == 'CORRECTION_OPEN'
    included = {row['ticket'] for row in sample['netting']['included_tickets']}
    assert '00010' not in included


def test_partial_closure_still_issues_closed_tickets(sample):
    row = next(r for r in sample['netting']['entities'] if r['entity'] == 'E-102')
    assert row['status'] == '可出令'
    assert row['closure'] == '部分闭合'
    assert row['included'] == 6 and row['deferred'] == 6


def test_all_confirmed_waits_for_whole_entity(sample):
    netting = evaluate({'dataset': 'dirty', 'policy': 'all_confirmed'})['netting']
    rows = {row['entity']: row for row in netting['entities']}
    assert rows['E-101']['status'] == '可出令'
    assert rows['E-102']['status'] == '暂缓' and rows['E-102']['net'] == ''
    assert rows['E-103']['status'] == '暂缓'
    assert netting['summary']['issued'] == 1


def test_identity_conflict_blocks_entity(clean):
    netting = evaluate({'dataset': 'clean', 'overrides': {
        'entities': [{'ticket': '00003', 'entity': 'E-101'}, {'ticket': '00003', 'entity': 'E-999'}]}})['netting']
    identity = [row for row in netting['held_tickets'] if row['code'] == 'ENTITY_CONFLICT']
    assert identity and all(row['ticket'] == '00003' for row in identity)
    assert all(row['entity'] not in ('E-101', 'E-999') or row['status'] == '暂缓' for row in netting['entities'])
    assert netting['basis']['net_total'] != evaluate({'dataset': 'clean'})['netting']['basis']['net_total']


def test_conflicting_boat_registration_blocks_entity(clean):
    netting = evaluate({'dataset': 'clean', 'overrides': {'boat_charges': [
        {'ref': 'SHIP-101', 'entity': 'E-101', 'amount': '1800.00', 'state': 'closed'},
        {'ref': 'SHIP-101', 'entity': 'E-101', 'amount': '1900.00', 'state': 'closed'}]}})['netting']
    row = next(r for r in netting['entities'] if r['entity'] == 'E-101')
    assert row['status'] == '暂缓'
    assert netting['assignment_problems'][0]['code'] == 'CHARGE_CONFLICT'


def test_ticket_unbalanced_blocks_entity(sample):
    state = {**sample}
    state['final'] = [dict(ticket, due='0.01') if ticket['ticket'] == '00001' else ticket for ticket in sample['final']]
    netting = netting_decision(state)
    entity = next(row for row in netting['entities'] if row['entity'] == 'E-101')
    assert entity['status'] == '暂缓' and entity['net'] == ''
    assert any(row['code'] == 'TICKET_UNBALANCED' for row in netting['held_tickets'])


def test_archive_mismatch_blocks_issue(sample):
    from app.modules.archive.rules import archive_decision
    drifted = {**sample['netting']['basis'], 'receivable_total': '1.00'}
    state = {**sample, 'netting_effective': {'command_id': 'NET-old', 'run_id': 'older',
                                             'policy': 'closed_first',
                                             'summary': {'net_total': '1.00'}, 'basis': drifted}}
    assert archive_decision(state)['status'] == '待处理'


def test_rerun_is_stable(sample, clean):
    again = evaluate()
    assert again['netting']['command_id'] == sample['netting']['command_id']
    assert again['netting']['basis'] == sample['netting']['basis']
    assert clean['netting']['summary']['net_total'] == '18040.41'


def test_policy_changes_command_not_amount(clean):
    netting = evaluate({'dataset': 'clean', 'policy': 'all_confirmed'})['netting']
    assert netting['policy'] == 'all_confirmed'
    assert netting['summary']['net_total'] == clean['netting']['summary']['net_total']
    assert netting['command_id'] != clean['netting']['command_id']


def test_verify_detects_included_duplicate(sample):
    command = {**sample['netting'], 'basis': {**sample['netting']['basis']}}
    command['basis']['included_tickets'] = sample['netting']['basis']['included_tickets'] + \
        sample['netting']['basis']['included_tickets'][:1]
    assert verify_command(sample, command)['ok'] is False


def test_unknown_policy_rejected():
    from app.errors import BusinessError
    import pytest
    with pytest.raises(BusinessError):
        evaluate({'policy': 'whatever'})
