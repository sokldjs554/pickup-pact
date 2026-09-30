"""Navigation shared by manual-choice browser suites after entry simplification."""


def reveal_manual_choices(page):
    details = page.locator('#customizeOrder')
    if details.get_attribute('open') is None:
        details.locator('summary').click()
