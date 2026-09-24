from test_journey import ordered, accepted

def test_late_pickup_estimate_never_pretends_the_customer_left_in_the_past():
    s=ordered(deadline_minutes=35)
    s=accepted(s,'advance',minutes=max(0,s['order']['plan']['start_at']-s['clock']))
    s=accepted(s,'start')
    s=accepted(s,'advance',minutes=s['order']['ready_at']-s['clock'])
    s=accepted(s,'ready')
    s=accepted(s,'advance',minutes=10)
    assert s['current_plan']['pickup_at']>=s['clock']
    assert s['current_plan']['arrival_at']>=s['clock']+s['current_plan']['walk_after']
    s=accepted(s,'claim',pickup_code=s['order']['pickup_code'])
    actual_pickup=s['clock']
    assert s['current_plan']['pickup_at']==actual_pickup
    s=accepted(s,'advance',minutes=1)
    assert s['current_plan']['pickup_at']==actual_pickup
