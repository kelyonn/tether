"""The live view in a real browser: navigation, focus, editing and keyboard use."""

from playwright.sync_api import expect


def side_projects(page):
    return page.locator("#side a.nav[href^='#/p/'] .lbl").all_inner_texts()


def test_overview_shows_every_active_project(page):
    expect(page.locator(".pcard")).to_have_count(2)
    assert side_projects(page) == ["Alpha", "Beta"]
    assert page.js_errors == []


def test_focus_narrows_everything_and_show_all_restores_it(page):
    page.goto(page.url.split("#")[0] + "#/p/alpha")
    page.get_by_role("button", name="Focus on this").click()
    assert side_projects(page) == ["Alpha"]
    page.locator("#side a.nav", has_text="Overview").click()
    expect(page.locator(".pcard")).to_have_count(1)
    page.get_by_role("button", name="Focused · show all 2").click()
    expect(page.locator(".pcard")).to_have_count(2)
    assert side_projects(page) == ["Alpha", "Beta"]


def test_focus_is_remembered_after_a_reload(page):
    page.goto(page.url.split("#")[0] + "#/p/beta")
    page.get_by_role("button", name="Focus on this").click()
    page.reload()
    page.wait_for_selector("#side .brand")
    assert side_projects(page) == ["Beta"]


def test_task_status_cycles_and_is_saved(page, seeded):
    page.goto(page.url.split("#")[0] + "#/p/alpha")
    row = page.locator(".row", has_text="alpha task one")
    row.locator("button.st").click()          # todo -> doing
    expect(page.locator("body")).to_contain_text("In progress")
    pid = seeded.find_project("alpha")["id"]
    assert [t["status"] for t in seeded.tasks(pid)] == ["doing"]


def test_adding_a_task_from_the_composer(page, seeded):
    page.goto(page.url.split("#")[0] + "#/p/beta")
    box = page.locator("#newtask")
    box.fill("write the launch post")
    box.press("Enter")
    expect(page.locator("body")).to_contain_text("write the launch post")
    pid = seeded.find_project("beta")["id"]
    assert "write the launch post" in [t["title"] for t in seeded.tasks(pid)]


def test_capturing_and_archiving_an_idea(page, seeded):
    page.goto(page.url.split("#")[0] + "#/ideas")
    page.locator("#newidea").fill("Mug subscription")
    page.get_by_role("button", name="Save idea").click()
    expect(page.locator("body")).to_contain_text("Mug subscription")
    assert any(m["title"] == "Mug subscription" for m in seeded.memories())
    page.locator(".icard", has_text="Mug subscription").locator("[data-act=archive]").click()
    expect(page.locator("body")).not_to_contain_text("Mug subscription")
    assert all(m["title"] != "Mug subscription" for m in seeded.memories())


def test_command_palette_opens_with_the_keyboard_and_finds_things(page):
    page.keyboard.press("Control+k")
    expect(page.locator("#palwrap")).to_have_class("pal-wrap on")
    page.locator("#palin").fill("alpha")
    page.wait_for_selector(".pal-item")
    page.keyboard.press("Escape")
    expect(page.locator("#palwrap")).not_to_have_class("pal-wrap on")


def test_unknown_project_shows_a_friendly_message(page):
    page.goto(page.url.split("#")[0] + "#/p/ghost")
    expect(page.get_by_text("Project not found")).to_be_visible()


def test_page_works_at_phone_width(page):
    page.set_viewport_size({"width": 390, "height": 800})
    expect(page.locator(".pcard").first).to_be_visible()
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1


def test_keyboard_shortcuts(page):
    page.keyboard.press("?")
    expect(page.locator("#helpwrap")).to_have_class("pal-wrap on")
    page.keyboard.press("Escape")
    expect(page.locator("#helpwrap")).not_to_have_class("pal-wrap on")
    page.keyboard.press("g")
    page.keyboard.press("i")
    expect(page.locator("#newidea")).to_be_visible()
    page.keyboard.press("g")
    page.keyboard.press("o")
    expect(page.locator(".pcard").first).to_be_visible()
    page.keyboard.press("c")
    expect(page.locator("#newidea")).to_be_focused()
    page.keyboard.press("Escape")
    page.locator("#newidea").blur()
    page.keyboard.press("/")
    expect(page.locator("#palwrap")).to_have_class("pal-wrap on")


def test_shortcuts_do_not_fire_while_typing(page):
    page.goto(page.url.split("#")[0] + "#/ideas")
    box = page.locator("#newidea")
    box.fill("")
    box.type("cgf?/")
    expect(box).to_have_value("cgf?/")
    expect(page.locator("#helpwrap")).not_to_have_class("pal-wrap on")


def test_focus_shortcut_toggles_the_focused_project(page):
    page.goto(page.url.split("#")[0] + "#/p/alpha")
    page.keyboard.press("f")
    assert side_projects(page) == ["Alpha"]
    page.keyboard.press("f")
    assert side_projects(page) == ["Alpha", "Beta"]


def test_archive_page_lists_and_restores(page, seeded):
    m = seeded.add_memory("Shelved thought", "later")
    seeded.archive_memory(m["id"])
    page.goto(page.url.split("#")[0] + "#/archive")
    page.reload()
    expect(page.locator("body")).to_contain_text("Shelved thought")
    expect(page.locator("body")).to_contain_text("Archived by you")
    page.get_by_role("button", name="Restore").click()
    expect(page.locator("body")).not_to_contain_text("Shelved thought")
    assert seeded.memory(m["id"])["archived"] == 0


def test_empty_archive_explains_itself(page):
    page.goto(page.url.split("#")[0] + "#/archive")
    expect(page.locator("body")).to_contain_text("Nothing is archived")
