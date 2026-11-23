"""主体净额：同一主体的应收货款与应付船款按户轧差，生成可追溯的净额指令。

设计要点：
- 票级闭合闸门：更正未闭合、复秤/扣杂隔离、版本冲突、同票金额无法对平时，该票暂缓。
- 主体级闸门：主体身份冲突、船款登记冲突时整户暂缓；净额与归档口径不符时整批暂缓。
- 净额口径：
  * closed_first（默认）——已闭合票先出指令，未闭合票单独列出暂缓，迟到更正只会替换对应票。
  * all_confirmed——同一主体全部票与船款确认后才轧差。
- 指令号只由纳入票、暂缓票、船款与更正版本等业务事实决定，重跑不新增指令、不累加净额。
"""
import hashlib
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from app.coerce import money
from app.errors import BusinessError
from app.manifest import digest

POLICIES = {'closed_first': '已闭合票先出令', 'all_confirmed': '全票确认后轧差'}
CLOSED_STATES = {'closed', 'confirmed', 'final', '已闭合', '已确认'}
PRIORITY = ['ENTITY_CONFLICT', 'VERSION_CONFLICT', 'TICKET_UNBALANCED', 'CORRECTION_OPEN', 'QUARANTINE_ROW', 'NOT_SETTLED']


def _dec(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _list(value):
    return value if isinstance(value, list) else []


def parse_assignments(raw):
    """主体登记：票号 -> 主体。同一票号登记到多个主体视为身份冲突。"""
    items = [{'ticket': k, 'entity': v} for k, v in raw.items()] if isinstance(raw, dict) else _list(raw)
    assignments = []
    problems = []
    for item in items:
        if not isinstance(item, dict):
            problems.append({'ticket': '', 'entity': '', 'code': 'ENTITY_CONFLICT', 'detail': '主体登记格式错误'})
            continue
        ticket = str(item.get('ticket', '')).strip()
        entity = str(item.get('entity', '')).strip()
        if not ticket or not entity:
            problems.append({'ticket': ticket, 'entity': entity, 'code': 'MISSING_ENTITY', 'detail': '主体登记缺少票号或主体'})
            continue
        assignments.append({'ticket': ticket, 'entity': entity})
    by_ticket = defaultdict(set)
    for item in assignments:
        by_ticket[item['ticket']].add(item['entity'])
    conflicts = {ticket: sorted(entities) for ticket, entities in by_ticket.items() if len(entities) > 1}
    return assignments, conflicts, problems


def parse_charges(raw):
    """船款登记：ref 唯一；同 ref 金额或主体不一致视为冲突，金额无效同样阻断主体。"""
    charges, problems, conflicts = [], [], set()
    seen = {}
    for item in _list(raw):
        if not isinstance(item, dict):
            problems.append({'ref': '', 'entity': '', 'code': 'CHARGE_CONFLICT', 'detail': '船款登记格式错误'})
            continue
        ref = str(item.get('ref', '')).strip()
        entity = str(item.get('entity', '')).strip()
        amount = _dec(item.get('amount'))
        if not ref or not entity:
            problems.append({'ref': ref, 'entity': entity, 'code': 'CHARGE_CONFLICT', 'detail': '船款缺少登记号或主体'})
            if entity:
                conflicts.add(entity)
            continue
        if amount is None or amount < 0:
            problems.append({'ref': ref, 'entity': entity, 'code': 'CHARGE_CONFLICT', 'detail': '船款金额无效'})
            conflicts.add(entity)
            continue
        closed_raw = item.get('state', item.get('confirmed', ''))
        closed = closed_raw if isinstance(closed_raw, bool) else str(closed_raw).strip() in CLOSED_STATES
        charge = {'ref': ref, 'entity': entity, 'amount': money(amount), 'closed': closed,
                  'revision': str(item.get('revision', 1))}
        if ref in seen:
            prior = seen[ref]
            if prior['amount'] != charge['amount'] or prior['entity'] != charge['entity']:
                conflicts.update({entity, prior['entity']})
                problems.append({'ref': ref, 'entity': entity, 'code': 'CHARGE_CONFLICT',
                                 'detail': '同一船款登记号金额或主体冲突'})
            continue
        seen[ref] = charge
        charges.append(charge)
    return charges, problems, conflicts


def _closure_index(state, pending_raw):
    quarantine, conflicts, pending = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in state.get('quarantine', []):
        ticket = str((row.get('raw') or {}).get('ticket', '')).strip()
        if ticket:
            quarantine[ticket].append(row)
    for row in state.get('version_conflicts', []):
        ticket = str(row.get('ticket', '')).strip()
        if ticket:
            conflicts[ticket].append(row)
    for item in _list(pending_raw):
        if not isinstance(item, dict):
            continue
        ticket = str(item.get('ticket', '')).strip()
        if ticket:
            pending[ticket].append(item)
    return quarantine, conflicts, pending


def ticket_issues(ticket, final, quarantine, conflicts, pending):
    """同一票能否参与轧差；返回原因列表，空列表表示闭合。"""
    issues = []
    for item in pending.get(ticket, []):
        issues.append({'code': 'CORRECTION_OPEN',
                       'detail': str(item.get('detail') or item.get('ref') or '迟到复秤或扣杂更正未闭合')})
    for row in quarantine.get(ticket, []):
        issues.append({'code': 'QUARANTINE_ROW', 'detail': f"{row.get('code', '')} 原始行 {row.get('line', '')}"})
    for row in conflicts.get(ticket, []):
        issues.append({'code': 'VERSION_CONFLICT', 'detail': str(row.get('code', '更正版本冲突'))})
    current = final.get(ticket)
    if current is None:
        issues.append({'code': 'NOT_SETTLED', 'detail': '票尚未形成生效版本'})
    else:
        amount, paid, due = (_dec(current.get('amount')), _dec(current.get('paid')), _dec(current.get('due')))
        if amount is None or paid is None or due is None:
            issues.append({'code': 'TICKET_UNBALANCED', 'detail': '金额字段无法解析'})
        elif amount != paid + due:
            issues.append({'code': 'TICKET_UNBALANCED', 'detail': f'应付 {amount} ≠ 已付 {paid} + 待付 {due}'})
    return sorted(issues, key=lambda issue: PRIORITY.index(issue['code']) if issue['code'] in PRIORITY else 99)


def _ticket_row(ticket, final, issues, entity):
    current = final.get(ticket) or {}
    code = issues[0]['code'] if issues else ''
    return {'entity': entity, 'ticket': ticket, 'version': current.get('version', ''),
            'amount': current.get('amount', ''), 'paid': current.get('paid', ''),
            'due': current.get('due', ''), 'lines': current.get('lines', []) if not issues else [],
            'code': code, 'detail': '；'.join(issue['detail'] for issue in issues)}


def _direction(net):
    if net > 0:
        return '划付主体'
    if net < 0:
        return '向主体收取'
    return '无需划转'


def netting_decision(state):
    policy = str(state.get('policy') or 'closed_first')
    if policy not in POLICIES:
        raise BusinessError('INVALID_POLICY', '净额口径无效')
    context = state.get('context') or {}

    final = {ticket['ticket']: ticket for ticket in state.get('final', [])}
    assignments, entity_conflicts, assignment_problems = parse_assignments(context.get('entities'))
    ticket_entities = defaultdict(set)
    for item in assignments:
        ticket_entities[item['ticket']].add(item['entity'])
    quarantine, version_conflicts, pending = _closure_index(state, context.get('pending_corrections'))
    charges, charge_problems, charge_conflicts = parse_charges(context.get('boat_charges'))
    names = context.get('entity_names') if isinstance(context.get('entity_names'), dict) else {}

    known_entities = ({item['entity'] for item in assignments} |
                      {charge['entity'] for charge in charges})
    rows = []
    included_tickets, held_tickets = [], []
    included_charges, held_charges = [], []

    for entity in sorted(known_entities):
        ticket_rows = []
        blocked = entity in charge_conflicts
        identity_conflict = False
        for ticket in sorted(t for t, entities in ticket_entities.items() if entity in entities):
            if ticket in entity_conflicts:
                issues = [{'code': 'ENTITY_CONFLICT',
                           'detail': '票号登记到多个主体：' + ' / '.join(entity_conflicts[ticket])}]
                identity_conflict = True
                blocked = True
            else:
                issues = ticket_issues(ticket, final, quarantine, version_conflicts, pending)
            if any(issue['code'] == 'TICKET_UNBALANCED' for issue in issues):
                # 同票金额无法对平：该票所在主体的净额不完整，整户暂缓。
                blocked = True
            ticket_rows.append(_ticket_row(ticket, final, issues, entity))

        entity_charges = [charge for charge in charges if charge['entity'] == entity]
        closed_charges = [charge for charge in entity_charges if charge['closed']]
        open_charges = [charge for charge in entity_charges if not charge['closed']]
        open_tickets = [row for row in ticket_rows if row['code']]

        if blocked:
            if identity_conflict:
                closure, block_code = '身份冲突', 'ENTITY_CONFLICT'
            elif any(row['code'] == 'TICKET_UNBALANCED' for row in ticket_rows):
                closure, block_code = '金额不平', 'TICKET_UNBALANCED'
            else:
                closure, block_code = '船款冲突', 'CHARGE_CONFLICT'
            status = '暂缓'
            picked, picked_charges = [], []
            # 有自身原因的票保留原码；其余票标注受整户阻断，避免把平账票误标成金额不平。
            held = [{**row, 'code': row['code'] or 'ENTITY_BLOCKED',
                     'detail': row['detail'] or f'{closure}，本户整户暂缓'} for row in ticket_rows]
        elif policy == 'all_confirmed' and (open_tickets or open_charges):
            status, closure, picked, picked_charges, held = '暂缓', '未闭合', [], [], ticket_rows
        else:
            picked = [row for row in ticket_rows if not row['code']]
            picked_charges = closed_charges
            held = open_tickets
            closure = '已闭合' if not held and not open_charges else '部分闭合'
            status = '可出令' if (picked or picked_charges) else '暂缓'

        block_code = block_code if blocked else 'CHARGE_OPEN'
        block_detail = f'{closure}，整户暂缓' if blocked else '船款未确认，暂缓'
        deferred_charges = entity_charges if blocked else open_charges

        receivable = sum((Decimal(final[row['ticket']]['due']) for row in picked), Decimal(0))
        payable = sum((Decimal(charge['amount']) for charge in picked_charges), Decimal(0))
        net = receivable - payable
        rows.append({'entity': entity, 'name': str(names.get(entity, entity)), 'policy': policy,
                     'closure': closure, 'status': status,
                     'receivable': money(receivable), 'payable': money(payable),
                     'net': money(net) if status == '可出令' else '',
                     'direction': _direction(net) if status == '可出令' else '暂缓',
                     'included': len(picked), 'deferred': len(held),
                     'deferred_charges': len(deferred_charges),
                     'basis_refs': ' '.join([row['ticket'] for row in picked] +
                                            [charge['ref'] for charge in picked_charges])})
        included_tickets.extend(picked)
        held_tickets.extend(held)
        included_charges.extend(picked_charges)
        held_charges.extend({'entity': charge['entity'], 'ref': charge['ref'], 'amount': charge['amount'],
                             'revision': charge['revision'], 'code': block_code, 'detail': block_detail}
                            for charge in deferred_charges)

    balance = state.get('balance') or {}
    global_blocks = []
    if balance.get('row_balanced') is False:
        global_blocks.append({'code': 'ROW_UNBALANCED', 'detail': '输入行未守恒，整批暂缓'})
    if balance.get('money_balanced') is False:
        global_blocks.append({'code': 'MONEY_UNBALANCED', 'detail': '应付 ≠ 已付 + 待付，整批暂缓'})
    if global_blocks:
        for row in rows:
            row['status'], row['net'], row['direction'] = '暂缓', '', '暂缓'

    issued = [row for row in rows if row['status'] == '可出令']
    chain_index = defaultdict(list)
    for audit in state.get('version_audit', []):
        chain_index[audit['ticket']].append({'version': audit['version'], 'replaces': audit.get('replaces', ''),
                                             'amount': audit.get('amount', ''), 'lines': audit.get('lines', [])})
    scoped = {row['ticket'] for row in included_tickets} | {row['ticket'] for row in held_tickets}
    revision_chains = [{'ticket': ticket, 'versions': sorted(chain_index[ticket], key=lambda item: item['version']),
                        'effective': final.get(ticket, {}).get('version', '')}
                       for ticket in sorted(scoped) if chain_index.get(ticket)]
    revision_rows = [{'ticket': chain['ticket'], 'version': item['version'],
                      'replaces': item.get('replaces', ''), 'amount': item.get('amount', ''),
                      'lines': item.get('lines', []),
                      'effective': '生效' if str(item['version']) == str(chain['effective']) else ''}
                     for chain in revision_chains for item in chain['versions']]
    assigned = {item['ticket'] for item in assignments}
    unassigned = [{'ticket': ticket['ticket'], 'version': ticket['version'], 'amount': ticket['amount'],
                   'due': ticket['due'], 'detail': '票未登记主体，无法纳入任何主体净额'}
                  for ticket in state.get('final', []) if ticket['ticket'] not in assigned]
    # 未登记主体的生效票不静默丢弃：列入暂缓，待补登记后再轧差。
    held_tickets = held_tickets + [{'entity': '—', 'ticket': row['ticket'], 'version': row['version'],
                                    'amount': row['amount'], 'paid': '', 'due': row['due'],
                                    'lines': [], 'code': 'UNASSIGNED', 'detail': row['detail']}
                                   for row in unassigned]

    basis = {
        'policy': policy, 'policy_label': POLICIES[policy],
        'run_sha256': digest(state.get('raw_bytes', b''), {**state.get('context', {}), 'netting_policy': policy}),
        'included_tickets': [{'entity': row['entity'], 'ticket': row['ticket'], 'version': row['version'],
                              'amount': row['amount'], 'paid': row['paid'], 'due': row['due'],
                              'lines': row['lines']} for row in included_tickets],
        'held_tickets': [{'entity': row['entity'], 'ticket': row['ticket'], 'code': row['code'],
                          'detail': row['detail']} for row in held_tickets],
        'charges_included': [{'entity': charge['entity'], 'ref': charge['ref'], 'amount': charge['amount'],
                              'revision': charge['revision']} for charge in included_charges],
        'charges_held': [{'entity': charge['entity'], 'ref': charge['ref'], 'amount': charge['amount'],
                          'revision': charge['revision']} for charge in held_charges],
        'revision_chains': revision_chains,
        'unassigned_tickets': [{'ticket': row['ticket'], 'due': row['due'], 'detail': row['detail']}
                               for row in unassigned],
        'receivable_total': money(sum((Decimal(row['due']) for row in included_tickets), Decimal(0))),
        'payable_total': money(sum((Decimal(charge['amount']) for charge in included_charges), Decimal(0))),
    }
    basis['net_total'] = money(Decimal(basis['receivable_total']) - Decimal(basis['payable_total']))
    fingerprint = hashlib.sha256(json.dumps({**basis, 'lineage': state.get('lineage', ''),
                                             'blocks': [block['code'] for block in global_blocks],
                                             'entity_status': [[row['entity'], row['status']] for row in rows]},
                                            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    command = {
        'command_id': 'NET-' + fingerprint[:12],
        'fingerprint': fingerprint,
        'policy': policy, 'policy_label': POLICIES[policy],
        'status': '可出指令' if issued and not global_blocks else '暂缓',
        'entities': rows, 'included_tickets': included_tickets, 'held_tickets': held_tickets,
        'charges_included': included_charges, 'charges_held': held_charges,
        'revision_chains': revision_chains, 'revision_rows': revision_rows,
        'global_blocks': global_blocks,
        'unassigned_tickets': unassigned,
        'assignment_problems': assignment_problems + charge_problems,
        'basis': basis, 'checks': None,
        'summary': {'entities': len(rows), 'issued': len(issued),
                    'included': len(included_tickets), 'held': len(held_tickets),
                    'net_total': basis['net_total']},
    }
    command['checks'] = verify_command(state, command)
    if not command['checks']['ok']:
        command['status'] = '暂缓'
        command['global_blocks'] = command['global_blocks'] + [{
            'code': 'NETTING_UNVERIFIED', 'detail': '净额与归档核对不一致：' + '、'.join(command['checks']['reasons'])}]
        for row in command['entities']:
            row['status'], row['net'], row['direction'] = '暂缓', '', '暂缓'
        command['summary']['issued'] = 0
    return command


def verify_command(state, command):
    """独立复核：指令中的票、版本、金额必须与归档账（final）一致，净额等于应收减应付。"""
    reasons = []
    if not command:
        return {'ok': False, 'reasons': ['NO_NETTING']}
    final = {ticket['ticket']: ticket for ticket in state.get('final', [])}
    total = Decimal(0)
    seen = set()
    for item in command['basis']['included_tickets']:
        ticket = final.get(item['ticket'])
        if ticket is None:
            reasons.append('INCLUDED_NOT_FINAL:' + str(item['ticket']))
            continue
        if str(ticket['version']) != str(item['version']) or ticket['due'] != item['due'] or ticket['amount'] != item['amount']:
            reasons.append('INCLUDED_DRIFT:' + str(item['ticket']))
        if item['ticket'] in seen:
            reasons.append('INCLUDED_DUPLICATE:' + str(item['ticket']))
        seen.add(item['ticket'])
        total += Decimal(ticket['due'])
    if money(total) != command['basis']['receivable_total']:
        reasons.append('RECEIVABLE_TOTAL')
    payable = sum((Decimal(charge['amount']) for charge in command['basis']['charges_included']), Decimal(0))
    if money(payable) != command['basis']['payable_total']:
        reasons.append('PAYABLE_TOTAL')
    if money(total - payable) != command['basis']['net_total']:
        reasons.append('NET_TOTAL')
    for row in command.get('entities', []):
        if row['status'] != '可出令':
            continue
        if row['net'] in ('', None):
            reasons.append('ENTITY_NET_MISSING:' + str(row['entity']))
            continue
        if money(Decimal(row['receivable']) - Decimal(row['payable'])) != row['net']:
            reasons.append('ENTITY_NET:' + str(row['entity']))
    return {'ok': not reasons, 'reasons': reasons}


def apply(state):
    netting = netting_decision(state)
    state['netting'] = netting
    state['views']['netting'] = {
        'title': '主体净额',
        'rows': netting['entities'],
        'metrics': {'可出令主体': netting['summary']['issued'], '纳入票': netting['summary']['included'],
                    '暂缓票': netting['summary']['held'], '净额_CNY': netting['summary']['net_total']},
    }
