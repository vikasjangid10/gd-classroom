"""Browser smoke test of the whole product.

Drives five real browsers — the host plus four participants — through classroom
creation, in-app invitation, acceptance, the live room and the recap, asserting on what
a user actually sees rather than on HTTP status codes.

Nothing is emailed. The host picks four registered participants; each of them is signed
in with a tab already open, and the invitation has to appear there on its own, over the
lobby event stream. That is the assertion the whole first half of this test exists for:
if the notification does not arrive without a reload, this fails.

    docker run --rm --shm-size=1g --network gd-classroom_default \
      -v "$PWD/frontend/e2e:/e2e" -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
      mcr.microsoft.com/playwright/python:v1.47.0-jammy \
      sh -c "pip install -q playwright==1.47.0 && python /e2e/ui_smoke.py"
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time

from playwright.async_api import Browser, Page, async_playwright

WEB = "http://localhost:5173"
PASSWORD = "Password123!"
SHOTS = "/e2e/shots"

#: Seeded participant accounts. The host chooses four of these in the picker.
PARTICIPANTS = [
    "priya@gdclassroom.io",
    "arjun@gdclassroom.io",
    "meera@gdclassroom.io",
    "dev@gdclassroom.io",
]


def host_resolver_rules() -> str:
    """Point the browser's own resolver at the compose services.

    The app is configured with the developer's origins (localhost:5173 and
    localhost:8000), so the test must use those exact URLs or it stops exercising the
    real CORS and cookie behaviour. Rather than proxying the traffic through this
    process — which becomes the bottleneck once several event streams are open — the
    browser is told where "localhost" really lives.
    """
    web_ip = socket.gethostbyname("web")
    api_ip = socket.gethostbyname("api")
    return f"MAP localhost:5173 {web_ip}:5173,MAP localhost:8000 {api_ip}:8000"


ANSWERS = [
    "Retrieval quality dominates everything else here. Chunk size and overlap decide "
    "recall long before the model does, and we measured that across three corpora.",
    "Evaluation is the part teams skip. Without a labelled set you are trading one kind "
    "of guesswork for another, so we built one before touching the retriever.",
    "It depends.",
    "Latency is a product feature. Streaming the first sentence into synthesis instead "
    "of waiting for the full completion removed about a second of dead air per turn.",
]


def ok(message: str) -> None:
    print(f"  \033[32mPASS\033[0m {message}", flush=True)


async def sign_in(browser: Browser, address: str, shot: str) -> Page:
    """One page per person.

    Each person gets their own *browser*, not just a context. Chromium partitions its
    six-connections-per-origin socket pool by top-level site, and contexts of the same
    browser share it — so five users sharing one browser starve each other's requests
    behind five long-lived event streams. Real users are on real separate browsers, and
    the test has to be too or it measures the test harness instead of the product.
    """
    page = await (await browser.new_context()).new_page()
    await page.goto(WEB, wait_until="domcontentloaded")
    await page.fill('input[type="email"]', address)
    await page.fill('input[type="password"]', PASSWORD)
    await page.click('button:has-text("Sign in")')
    await page.wait_for_selector('button:has-text("Sign out")', timeout=15_000)
    await page.screenshot(path=f"{SHOTS}/{shot}.png", full_page=True)
    return page


async def main() -> int:
    browsers: list[Browser] = []

    async with async_playwright() as pw:

        async def new_browser() -> Browser:
            instance = await pw.chromium.launch(
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    # Several browsers in one container: keep the footprint small.
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--js-flags=--max-old-space-size=128",
                    "--renderer-process-limit=1",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--blink-settings=imagesEnabled=false",
                    f"--host-resolver-rules={host_resolver_rules()}",
                ]
            )
            browsers.append(instance)
            return instance

        browser = await new_browser()

        print("\n\033[1m1. Host signs in\033[0m")
        host = await sign_in(browser, "super@gdclassroom.io", "01-login")
        await host.wait_for_selector("text=Start a discussion")
        ok("host lands on the classroom dashboard")

        # Close anything left open by a previous run — a classroom still holding its
        # four participants would (correctly) block them from joining a new one.
        closed = 0
        while closed < 12:
            open_room = host.locator(
                'a[href^="/classrooms/"]', has=host.locator("text=/inviting|ready|live/i")
            ).first
            if not await open_room.count():
                break
            await open_room.click()
            await host.wait_for_selector('button:has-text("Cancel classroom")', timeout=15_000)
            await host.click('button:has-text("Cancel classroom")')
            await host.wait_for_selector("text=/cancelled/i", timeout=15_000)
            await host.click('a:has-text("All classrooms")')
            await host.wait_for_selector("text=Start a discussion", timeout=15_000)
            closed += 1
        if closed:
            ok(f"cleared {closed} classroom(s) left open by an earlier run")

        title = f"Smoke run {int(time.time())}"

        # Every participant is signed in and looking at the app *before* the classroom
        # exists. Nothing below reloads their page: the invitation has to arrive on its
        # own or the notification path is broken.
        print("\n\033[1m2. Four participants are already signed in and idle\033[0m")
        people: list[Page] = []
        for index, address in enumerate(PARTICIPANTS):
            people.append(await sign_in(await new_browser(), address, f"04-participant-{index}"))
        ok("four participants signed in, no invitation waiting")

        print("\n\033[1m3. Host picks them and creates the classroom\033[0m")
        await host.click('button:has-text("Retrieval Augmented Generation")')
        for address in PARTICIPANTS:
            await host.click(f'[data-testid="invitee-{address}"]')
        picked = await host.locator('[data-testid="picked-count"]').inner_text()
        assert picked.strip().startswith("4"), f"picker shows {picked!r}"
        ok("host selected four registered participants from the app")

        await host.fill('input[placeholder*="Name this session"]', title)
        await host.screenshot(path=f"{SHOTS}/02-dashboard.png", full_page=True)
        await host.click('button:has-text("Create classroom")')
        await host.wait_for_selector("text=/inviting/i", timeout=20_000)

        await host.click(f'a:has-text("{title}")')
        await host.wait_for_selector("text=Acceptances")
        shown = {
            value.strip()
            for value in await host.locator('[data-testid="roster-email"]').all_inner_texts()
        }
        assert set(PARTICIPANTS) <= shown, f"roster shows {shown}, expected {PARTICIPANTS}"
        ok("roster lists exactly the four people the host chose")
        await host.screenshot(path=f"{SHOTS}/03-roster.png", full_page=True)

        print("\n\033[1m4. The invitation appears in their app, with no reload\033[0m")
        for index, page in enumerate(people):
            await page.wait_for_selector('[data-testid="invite-toast"]', timeout=25_000)
            if index == 0:
                ok("notification arrived over the lobby stream, unprompted")
                await page.screenshot(path=f"{SHOTS}/05-invitation.png", full_page=True)

        for page in people:
            await page.click('[data-testid="invite-toast"]')
            card = page.locator("div.card", has=page.get_by_text(title, exact=True)).first
            await card.wait_for(state="visible", timeout=20_000)
            await card.locator('button:has-text("Accept")').click()

        await people[-1].wait_for_selector("text=/Join the discussion/i", timeout=30_000)
        await people[0].wait_for_selector("text=/Join the discussion/i", timeout=30_000)
        ok("fourth acceptance opened the room for everyone")

        # No reload: the host's page must update from the event stream alone.
        await host.wait_for_selector('span:has-text("ready")', timeout=25_000)
        await host.wait_for_selector("text=4 / 4", timeout=10_000)
        ok("host's open page flipped to READY from the event stream alone")
        await host.screenshot(path=f"{SHOTS}/06-ready.png", full_page=True)

        print("\n\033[1m5. Everyone joins the room\033[0m")
        for index, page in enumerate(people):
            # Whoever completed the quorum is already in the room; the rest arrive via
            # the prompt their event stream raised.
            if "/room/" not in page.url:
                await page.click('button:has-text("Join the discussion")')
                await page.wait_for_selector("text=Discussion room", timeout=20_000)
            await page.click('button:has-text("Join without a microphone")')
            if index == 0:
                ok("room opens with the live transcript panel")

        await people[0].wait_for_selector("text=/has the floor|MODERATOR/i", timeout=40_000)
        ok("moderator introduced the topic and handed out the floor")
        await asyncio.sleep(4)
        await people[0].screenshot(path=f"{SHOTS}/07-room-live.png", full_page=True)

        print("\n\033[1m6. Participants take turns\033[0m")
        turns_taken = 0
        deadline = time.time() + 120
        while turns_taken < 5 and time.time() < deadline:
            # Whoever currently holds the floor is the only one whose box says so.
            holder = None
            for page in people:
                box = page.locator('input[placeholder*="the floor"]')
                if await box.count() and await box.is_visible():
                    holder = (page, box)
                    break
            if holder is None:
                await asyncio.sleep(1.5)  # the moderator is still speaking
                continue

            page, box = holder
            # Type rather than fill: this is a controlled React input, and typing is
            # what a participant actually does.
            await box.click()
            await box.press_sequentially(ANSWERS[turns_taken % len(ANSWERS)], delay=1)
            send = page.locator('button:has-text("Send turn")')
            if await send.is_disabled():
                print(f"      value in box: {await box.input_value()!r}", flush=True)
                raise AssertionError("Send turn stayed disabled after typing")
            async with page.expect_response(
                lambda r: "turn-text" in r.url and r.request.method == "POST", timeout=10_000
            ) as info:
                await send.click()
            response = await info.value
            assert response.status == 202, f"turn rejected: {response.status}"
            turns_taken += 1
            await asyncio.sleep(2.5)

        assert turns_taken >= 3, f"only {turns_taken} turns were taken"
        ok(f"{turns_taken} participant turns went through the moderator")

        transcript = await people[0].locator("aside div.animate-rise").count()
        assert transcript > 3, f"transcript only had {transcript} lines"
        ok(f"live transcript accumulated {transcript} lines")
        await people[0].screenshot(path=f"{SHOTS}/08-room-transcript.png", full_page=True)

        print("\n\033[1m7. Host ends it and the summary appears\033[0m")
        # The host is not one of the four: they watch the room and may close it.
        session_id = people[0].url.rsplit("/", 1)[1]
        await host.goto(f"{WEB}/room/{session_id}", wait_until="domcontentloaded")
        await host.wait_for_selector("text=You convened this discussion", timeout=20_000)
        assert not await host.locator('input[placeholder*="Type a turn"]').count(), (
            "the host was offered a way to speak"
        )
        assert not await people[0].locator('button:has-text("End discussion")').count(), (
            "a participant was offered the host's End discussion control"
        )
        ok("host observes without a seat; only they can end the discussion")

        await host.click('button:has-text("End discussion")')
        await people[0].wait_for_selector("text=Discussion finished", timeout=60_000)
        ok("room announced the end of the discussion")

        await people[0].click('button:has-text("Read the summary")')
        await people[0].wait_for_selector("text=Key points", timeout=30_000)
        points = await people[0].locator("section li").count()
        assert points >= 3, f"summary showed only {points} key points"
        ok(f"summary rendered with {points} key points and the full transcript")
        await people[0].screenshot(path=f"{SHOTS}/09-recap.png", full_page=True)

        for instance in browsers:
            await instance.close()
        print("\n\033[1m\033[32mUI SMOKE PASSED\033[0m\n")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001
        print(f"\n\033[31mUI SMOKE FAILED\033[0m: {type(exc).__name__}: {exc}\n")
        sys.exit(1)
