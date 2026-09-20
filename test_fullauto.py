"""Auto E self-checks: the full-auto cycle (keyboard stubbed out) and the mouse nudge.  py test_fullauto.py"""
import ctypes
import queue
import threading
import time
from collections import Counter, deque

import auto_e as a

sent = []
w = type("W", (), {"mode": "skill", "target": None, "armed": False, "last_sent": 0.0})()
cfg = {"skill": {"region": {"left": 0, "top": 0, "width": 10, "height": 10}}, "hold": 10,
       "auto": [{"on": True, "key": "E", "wait": "bar", "watch": True},
                {"on": True, "key": "1", "wait": "0.01", "watch": True},
                {"on": True, "key": "T", "wait": "0", "watch": True},
                {"on": True, "key": "e yoga", "wait": "0.01", "watch": False},
                {"on": True, "key": "X", "wait": "0", "watch": True},
                {"on": False, "key": "Backspace", "wait": "0", "watch": True}]}
auto = a.FullAuto(cfg, w)

armed_at = {}


def fake_press(ch, cfg, region, target=None):
    sent.append(ch)
    armed_at[ch] = w.armed
    if ch == "E":  # the watcher presses the bar's key a moment after E opens the bar
        threading.Timer(0.3, lambda: setattr(w, "last_sent", time.time())).start()
    return "stub"


a.press = fake_press

auto.on = True
t = threading.Thread(target=auto.run, daemon=True)
t.start()
while len(sent) < 11 and t.is_alive():
    time.sleep(0.05)
auto.on = False
t.join(2)

assert sent[:10] == ["E", "1", "T", "e", " ", "y", "o", "g", "a", "Enter"], sent
assert sent[10:11] in ([], ["X"]), sent  # X ends the emote, then the cycle starts over at E
# the region is watched while the bar is up, and not during the emote (that also takes the outline off)
assert armed_at["E"] and armed_at["1"] and armed_at["T"], armed_at
assert armed_at.get("Enter") is False, f"still watching the region during the emote: {armed_at}"
assert "Backspace" not in sent, "a step switched off still ran"
assert a.vk_of("Enter") == 0x0D and a.vk_of("1") == 0x31 and a.vk_of("T") == 0x54
print("ok:", " ".join(sent))

# a step switched off is skipped, and "0" goes straight on to the next step
sent.clear()
cfg["auto"] = [{"on": True, "key": "Q", "wait": "0", "watch": True},
               {"on": False, "key": "Backspace", "wait": "0", "watch": True},
               {"on": True, "key": "hello", "wait": "0", "watch": True}]
auto.on = True
t = threading.Thread(target=auto.run, daemon=True)
t.start()
while len(sent) < 8 and t.is_alive():
    time.sleep(0.05)
auto.on = False
t.join(2)
assert sent[:8] == ["Q", "h", "e", "l", "l", "o", "Enter", "Q"], sent
print("ok: steps off are skipped, text is typed and sent ->", " ".join(sent[:8]))

# the mouse nudge: the INPUT union must lay MOUSEINPUT over the same bytes as KEYBDINPUT, and a nudge has to
# come back to exactly where it started or the camera would drift a little further every time
assert ctypes.sizeof(a.INPUT) == ctypes.sizeof(a.MOUSEINPUT) + 8, ctypes.sizeof(a.INPUT)
assert a.INPUT.mi.offset == a.INPUT.ki.offset


home = a.cursor_at()
a.user32.SetCursorPos(400, 400)   # away from the screen edges, where a move would clip
before = a.cursor_at()
a.move_mouse(7, 6)
moved = a.cursor_at()
assert moved != before, "the mouse never moved: SendInput rejected the mouse INPUT"
a.user32.SetCursorPos(*before)
for _ in range(5):                # pointer acceleration is not symmetric, so this has to hold every time
    a.nudge_mouse()
    assert a.cursor_at() == before, f"the nudge drifted: {before} -> {a.cursor_at()}"
a.user32.SetCursorPos(*home)
print("ok: mouse moved to", moved, "and 5 nudges left the pointer on", before)

# a press aimed while watching, then queued, must not fire once watching has stopped -- otherwise the bar's
# key lands in the chat box that the next step just opened
w2 = a.Watcher.__new__(a.Watcher)
w2.cfg = {"delay": 0, "dev": False, "dry_run": False, "hold": 10, "background": False}
w2.armed, w2.target = True, None
w2.jobs, w2.log, w2.presses = queue.Queue(), deque(maxlen=10), Counter()
w2.last_press, w2.last_miss, w2.misses, w2.last_sent = (0.0, None), 0.0, 0, 0.0
fired = []
a.press = lambda letter, cfg, region, target=None: fired.append(letter) or "stub"  # noqa: replaces fake_press
threading.Thread(target=w2.press_loop, daemon=True).start()

w2.armed = False
w2.queue_press("E", {"left": 0, "top": 0, "width": 1, "height": 1}, "pressed")
time.sleep(0.3)
assert fired == [], f"a queued press fired after watching stopped: {fired}"
w2.armed = True
w2.queue_press("E", {"left": 0, "top": 0, "width": 1, "height": 1}, "pressed")
time.sleep(0.3)
assert fired == ["E"], f"a press while watching did not fire: {fired}"
print("ok: queued presses are dropped once watching stops")
