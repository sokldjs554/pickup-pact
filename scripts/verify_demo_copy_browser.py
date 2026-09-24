#!/usr/bin/env python3
"""Read revised copy from real HTTP pages; retain simulation and price disclosures."""
import argparse
import json
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


def verify(base, output, repeat=3, expected_commit=None, ops_url=None):
    output.mkdir(parents=True, exist_ok=True)
    with urlopen(base + '/health', timeout=30) as response:
        health = json.load(response)
    if expected_commit:
        assert health['release_commit'] == expected_commit, health
    reports = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for pass_no in range(1, repeat + 1):
            for mode, width, height in [('desktop', 1440, 1000), ('mobile', 390, 844)]:
                context = browser.new_context(viewport={'width': width, 'height': height}, locale='ko-KR')
                context.set_default_timeout(20000)
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))

                def screen(name):
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), (mode, name)
                    if pass_no == 1:
                        page.screenshot(path=str(output / f'{mode}-{name}.png'), full_page=True)

                page.goto(base + '/', wait_until='networkidle')
                expect(page.locator('h1').first).to_contain_text('커피 한 잔')
                expect(page.locator('.demo-strip')).to_contain_text('실제 주문이나 결제는 되지 않아요.')
                expect(page.locator('nav [data-view=customer]')).to_have_text('내 주문')
                expect(page.locator('#map circle')).not_to_have_count(0)
                screen('home')
                page.locator('#budget').select_option('3000')
                page.locator('#oat').check()
                page.locator('#findRoutes').click()
                expect(page.locator('#routeArea .empty-state')).to_be_visible()
                expect(page.locator('#routeArea')).to_contain_text('도착 시간이나 결제 한도')
                screen('no-matching-cafe')
                page.locator('nav [data-view=merchant]').click()
                expect(page.locator('#merchantContent')).to_contain_text('아직 받은 주문이 없어요.')
                page.locator('nav [data-view=receipt]').click()
                expect(page.locator('#receiptContent')).to_contain_text('아직 영수증이 없어요.')
                page.locator('#reset').click()
                page.wait_for_load_state('networkidle')
                page.locator('#coupon').select_option('wave1000')
                page.locator('#points').fill('1000')
                page.locator('#findRoutes').click()
                wave = page.locator('.route-card[data-hover=wave]')
                expect(wave).to_contain_text('2,300원')
                wave.locator('[data-quote]').click()
                expect(page.locator('#lockedIntent')).to_contain_text('할인이 달라지면 금액을 먼저 알려드려요.')
                page.locator('[data-action=busy]').click()
                page.locator('.route-card[data-hover=oat] [data-quote]').click()
                expect(page.locator('#dialogTitle')).to_have_text('이 매장에서 받아볼까요?')
                expect(page.locator('#couponLossWarning')).to_contain_text('쿠폰 할인 1,000원을 받지 못해요.')
                expect(page.locator('#dialogBody .benefit-total')).to_contain_text('3,700원')
                expect(page.locator('#dialogBody')).to_contain_text('1,400원')
                screen('coupon-loss-consent')
                page.locator('#dismissDialog').click()

                page.goto(base + '/classic', wait_until='networkidle')
                expect(page.locator('#menuList')).to_contain_text('아메리카노')
                screen('classic-customer')
                page.goto(base + '/classic?dev=1', wait_until='networkidle')
                expect(page.locator('.sidebar')).to_be_visible()
                expect(page.locator('body')).to_contain_text('운영 화면')
                screen('classic-operator')

                page.goto(base + '/repair-lab', wait_until='networkidle')
                expect(page.locator('h1')).to_have_text('취소된 주문, 정산도 맞게 처리됐을까요?')
                expect(page.locator('#decision')).to_have_text('처리할 내역이 있어요')
                page.locator('[data-case=partial_cancel]').click()
                page.locator('#preview').click()
                expect(page.locator('#planContent')).to_contain_text('3,000원')
                expect(page.locator('#events')).to_contain_text('부분취소 적용')
                screen('partial-repair')
                page.locator('[data-case=missing_parent]').click()
                expect(page.locator('#decision')).to_have_text('빠진 기록을 기다려요')
                expect(page.locator('#preview')).to_be_disabled()
                screen('waiting-for-record')
                if ops_url:
                    page.goto(ops_url, wait_until='networkidle')
                    expect(page.locator('h1')).to_contain_text('정산 기록부터 확인해요')
                    expect(page.locator('body')).to_contain_text('실제 결제나 환불은 하지 않아요.')
                    screen('separate-ops-console')
                assert not errors, errors
                reports.append({'pass': pass_no, 'viewport': mode, 'result': 'passed',
                    'release_commit': health.get('release_commit'), 'page_errors': errors,
                    'checks': ['real_http', 'natural_labels', 'empty_states', 'simulation_disclosure',
                               'coupon_loss_1000', 'price_difference_1400', 'cash_due_3700',
                               'classic_customer', 'classic_operator', 'repair_3000',
                               'wait_state', 'no_horizontal_overflow'],
                    'separate_ops_console': bool(ops_url)})
                (output/'browser-results.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2))
                context.close()
        browser.close()
    return reports


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:10000')
    parser.add_argument('--output', type=Path, default=Path('verification/copy-browser'))
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--expected-commit')
    parser.add_argument('--ops-url')
    args = parser.parse_args()
    assert 1 <= args.repeat <= 10
    print(json.dumps(verify(args.base_url.rstrip('/'), args.output, args.repeat, args.expected_commit, args.ops_url), ensure_ascii=False))
