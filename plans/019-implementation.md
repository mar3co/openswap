# API Credit Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Console API key linked to a subscription account becomes the auto-switch engine's last resort when every subscription is out of quota, the engine returns to a healthy subscription afterwards, and the extra gets a proper in-popover page for entering a token.

**Architecture:** The roster record of an API-key slot gains a `creditFor` identity link resolved to a parent slot at read time. The engine's eligibility rule becomes "linked, or the global flag", a `credit-return` trigger replaces the unhealthy-count detour off an active key, and the extra reads one new `credit_for` map from the snapshot. The token page is an AppKit page in `menubar_panel.py` driven by a pure state model in `menubar_display.py`.

**Tech Stack:** Python 3.12, pytest (`uv run pytest`), PyObjC/AppKit for the extra (never imported by tests), rumps.

**Spec:** `plans/019-api-credit-fallback.md`

## Global Constraints

- Tests never import AppKit or rumps; `tests/test_menubar.py` imports `openswap.menubar` only, and lifts `MenuBarApp` methods with `tests/menubar_harness.py`.
- User-visible copy never echoes tokens, paths, command lines, or raw exceptions.
- The extra re-implements no quota math, ranking, or credential write; it reads the snapshot and calls the engine façade.
- `_reject_cross_kind_collision` in `engine/switch.py` stays as is: the link is a separate record field, never a shared email.
- Subscriptions with room always beat keys; keys never target a consume-first nudge; all-exhausted fires only when no eligible key remains.
- American English spelling everywhere. Commit subjects are plain imperative with no attribution trailers.
- Run the full suite with `uv run pytest -q` before every commit; the suite must stay green.

## Review Focus

- A parent held out of rotation with `openswap disable`: the link still resolves, so its key stays eligible as a last resort (Task 2 test `test_linked_key_eligible_when_parent_disabled`).
- Two keys linked to the same parent: both eligible, lower slot first inside the "left account's key" group (Task 2 test `test_two_keys_same_parent_lower_slot_first`).
- A parent whose refresh token is dead when the engine wants to return: the parent is quarantined, the engine stays on the key with `on-api-credit` instead of counting unhealthy ticks (Task 2 test `test_credit_return_skips_quarantined_parent`).
- A parent with a non-empty organization uuid: the link stores and matches on that uuid, so an org account and a personal account sharing an email never cross (Task 1 test `test_link_matches_on_org_uuid`).
- A token pasted with surrounding whitespace or a trailing newline: detection runs on the stripped value and Add is enabled (Task 5 test `test_token_entry_state_strips_whitespace`).

---

### Task 1: Roster link

**Files:**
- Modify: `src/openswap/engine/switch.py:654-700` (`add_account_from_token`), `:701-712` (`_commit_add_account_from_token` signature), `:893-905` (record write), `:756-775` (refresh-in-place)
- Modify: `src/openswap/engine/slots.py:1008-1011` (next to `account_kind_for`), `:1251-1370` (`remove_account`)
- Modify: `src/openswap/engine/snapshot.py:138-176` (`_build_accounts_info`), `:63-87` (`accounts_snapshot`)
- Modify: `src/openswap/engine/protocol.py:31`
- Modify: `src/openswap/codex/engine.py:307-309`
- Modify: `src/openswap/models.py:131-141`
- Test: `tests/test_api_key_accounts.py`

**Interfaces:**
- Consumes: `IdentityMixin.resolve_account(identifier) -> (num, email, org_uuid)`, `IdentityMixin._find_account_slot(data, email, org_uuid) -> str | None`, `SlotsMixin._account_kind(num) -> "api_key" | "oauth"`.
- Produces:
  - `SwitchMixin.add_account_from_token(token, email=None, slot=None, assume_yes=False, credit_for: str | None = None) -> None`
  - `SlotsMixin.credit_parent_for(account_num: str) -> str | None` (also on `AccountEngine` Protocol; `CodexEngine` returns `None`)
  - `SlotsMixin._credit_parent_in(data: dict, account_num: str) -> str | None`
  - module function `openswap.engine.slots.order_keys_after_parents(sequence: list[int], parent_of: dict[str, str | None]) -> list[int]`
  - `AccountSnapshot.credit_for: str` (parent slot number or `""`)
  - roster record field `"creditFor": {"email": str, "organizationUuid": str}`

- [ ] **Step 1: Write the failing roster tests**

Append to `tests/test_api_key_accounts.py`:

```python
# ---------------------------------------------------------------------------
# API credit link (plan 019)
# ---------------------------------------------------------------------------


def _seed_oauth(s: ClaudeAccountSwitcher, num: int, email: str, org_uuid: str = "", alias: str = "") -> None:
    s._write_account_credentials(str(num), email, OAUTH_JSON)
    s._write_account_config(
        str(num), email,
        json.dumps({"oauthAccount": {"emailAddress": email, "accountUuid": f"u{num}",
                                     "organizationUuid": org_uuid or None}}),
    )
    data = s._get_sequence_data()
    record = {"email": email, "uuid": f"u{num}", "organizationUuid": org_uuid,
              "organizationName": "", "added": "2024-01-01T00:00:00Z"}
    if alias:
        record["alias"] = alias
    data["accounts"][str(num)] = record
    if num not in data["sequence"]:
        data["sequence"].append(num)
        data["sequence"].sort()
    s._write_json(s.sequence_file, data)


class TestCreditLink:
    def test_for_by_slot_stores_parent_identity(self, temp_home: Path, capsys):
        s = _linux_switcher()
        _seed_oauth(s, 1, "a@example.com")
        s.add_account_from_token(API_KEY, credit_for="1")
        record = s._get_sequence_data()["accounts"]["2"]
        assert record["kind"] == "api_key"
        assert record["creditFor"] == {"email": "a@example.com", "organizationUuid": ""}
        assert s.credit_parent_for("2") == "1"

    def test_for_by_email_and_alias(self, temp_home: Path, capsys):
        s = _linux_switcher()
        _seed_oauth(s, 1, "a@example.com", alias="work")
        s.add_account_from_token(API_KEY, credit_for="a@example.com")
        s.add_account_from_token(OTHER_KEY, credit_for="work")
        assert s.credit_parent_for("2") == "1"
        assert s.credit_parent_for("3") == "1"

    def test_link_matches_on_org_uuid(self, temp_home: Path, capsys):
        s = _linux_switcher()
        _seed_oauth(s, 1, "a@example.com")
        _seed_oauth(s, 2, "a@example.com", org_uuid="org-1")
        s.add_account_from_token(API_KEY, credit_for="2")
        assert s._get_sequence_data()["accounts"]["3"]["creditFor"]["organizationUuid"] == "org-1"
        assert s.credit_parent_for("3") == "2"

    def test_ambiguous_email_is_an_error(self, temp_home: Path):
        from openswap.exceptions import ConfigError
        s = _linux_switcher()
        _seed_oauth(s, 1, "a@example.com")
        _seed_oauth(s, 2, "a@example.com", org_uuid="org-1")
        with pytest.raises(ConfigError):
            s.add_account_from_token(API_KEY, credit_for="a@example.com")
        assert "3" not in s._get_sequence_data()["accounts"]

    def test_setup_token_cannot_be_linked(self, temp_home: Path):
        s = _linux_switcher()
        _seed_oauth(s, 1, "a@example.com")
        with pytest.raises(ValidationError, match="setup token"):
            s.add_account_from_token("sk-ant-oat01-abc", credit_for="1")

    def test_api_key_cannot_be_a_parent(self, temp_home: Path, capsys):
        s = _linux_switcher()
        s.add_account_from_token(API_KEY)
        with pytest.raises(ValidationError, match="API key"):
            s.add_account_from_token(OTHER_KEY, credit_for="1")

    def test_unknown_parent_is_not_found(self, temp_home: Path):
        from openswap.exceptions import AccountNotFoundError
        s = _linux_switcher()
        with pytest.raises(AccountNotFoundError):
            s.add_account_from_token(API_KEY, credit_for="9")

    def test_unlinked_key_has_no_parent(self, temp_home: Path, capsys):
        s = _linux_switcher()
        s.add_account_from_token(API_KEY)
        assert s.credit_parent_for("1") is None
        assert s.credit_parent_for("99") is None

    def test_removing_parent_orphans_key_and_prints_notice(self, temp_home: Path, capsys):
        s = _linux_switcher()
        _seed_oauth(s, 1, "a@example.com")
        s.add_account_from_token(API_KEY, credit_for="1")
        capsys.readouterr()
        s.remove_account("1", assume_yes=True)
        out = capsys.readouterr().out
        assert "Slot 2 (API credit for a@example.com) is now a standalone key." in out
        assert s.credit_parent_for("2") is None
        # The record keeps the link, so re-adding the parent resolves again.
        _seed_oauth(s, 1, "a@example.com")
        assert s.credit_parent_for("2") == "1"

    def test_link_survives_swap_and_move(self, temp_home: Path, capsys):
        s = _linux_switcher()
        _seed_oauth(s, 1, "a@example.com")
        _seed_oauth(s, 2, "b@example.com")
        s.add_account_from_token(API_KEY, credit_for="1")
        s.swap_accounts("1", "2")
        assert s.credit_parent_for("3") == "2"
        s.move_account("3", "7")
        assert s.credit_parent_for("7") == "2"

    def test_snapshot_carries_parent_and_orders_key_after_parent(self, temp_home: Path, capsys):
        s = _linux_switcher()
        _seed_oauth(s, 1, "a@example.com")
        _seed_oauth(s, 2, "b@example.com")
        s.add_account_from_token(API_KEY, credit_for="1")
        snap = s.accounts_snapshot(fetch=set())
        by_num = {a.number: a for a in snap.accounts}
        assert by_num["3"].credit_for == "1"
        assert by_num["1"].credit_for == ""
        assert [a.number for a in snap.accounts] == ["1", "3", "2"]


def test_order_keys_after_parents_keeps_strays():
    from openswap.engine.slots import order_keys_after_parents
    assert order_keys_after_parents([1, 2, 3, 4], {"1": None, "2": None, "3": "1", "4": "7"}) == [1, 3, 2, 4]
    assert order_keys_after_parents([], {}) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_key_accounts.py -k "CreditLink or order_keys" -q`
Expected: FAIL (`TypeError: ... unexpected keyword argument 'credit_for'`, `AttributeError: credit_parent_for`, `ImportError: order_keys_after_parents`).

- [ ] **Step 3: Add the roster accessors and the ordering helper**

In `src/openswap/engine/slots.py`, add at module level (after the imports, before `class SlotsMixin`):

```python
def order_keys_after_parents(
    sequence: list[int], parent_of: dict[str, str | None]
) -> list[int]:
    """Roster order with each linked key placed directly after its parent.

    A key whose parent is not in ``sequence`` keeps its own position.
    """
    children: dict[str, list[int]] = {}
    present = {str(num) for num in sequence}
    for num in sequence:
        parent = parent_of.get(str(num))
        if parent is not None and parent in present:
            children.setdefault(parent, []).append(num)
    placed: set[int] = set()
    ordered: list[int] = []
    for num in sequence:
        if num in placed:
            continue
        parent = parent_of.get(str(num))
        if parent is not None and parent in present:
            continue
        ordered.append(num)
        placed.add(num)
        for child in children.get(str(num), []):
            ordered.append(child)
            placed.add(child)
    ordered.extend(num for num in sequence if num not in placed)
    return ordered
```

In `SlotsMixin`, directly after `account_kind_for`:

```python
    def _credit_parent_in(self, data: dict, account_num: str) -> str | None:
        """Slot of the subscription account a key is API credit for, from loaded data."""
        record = data.get("accounts", {}).get(str(account_num), {})
        link = record.get("creditFor")
        if not isinstance(link, dict):
            return None
        return self._find_account_slot(
            data, link.get("email", ""), link.get("organizationUuid", "") or ""
        )

    def credit_parent_for(self, account_num: str) -> str | None:
        """Parent slot for a linked API key; ``None`` when unlinked or the parent is gone."""
        data = self._get_sequence_data() or {}
        return self._credit_parent_in(data, str(account_num))
```

In `src/openswap/engine/protocol.py`, after `def account_kind_for(...)`:

```python
    def credit_parent_for(self, account_num: str) -> str | None: ...
```

In `src/openswap/codex/engine.py`, after `account_kind_for`:

```python
    def credit_parent_for(self, account_num: str) -> str | None:
        return None
```

- [ ] **Step 4: Thread `credit_for` through add-from-token**

In `src/openswap/engine/switch.py`, change the `add_account_from_token` signature and body:

```python
    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
        credit_for: str | None = None,
    ) -> None:
```

Add to the docstring `Args`:

```
            credit_for: Slot number, email, or alias of the subscription account
                   this API key is API credit for. Only valid with an
                   ``sk-ant-api…`` key; a setup token is a login of its own.
```

After `is_api_key = looks_like_api_key(token)` and the email validation, before the commit call:

```python
        parent: tuple[str, str] | None = None
        if credit_for is not None:
            if not is_api_key:
                raise ValidationError(
                    "--for links an API key to an account; a setup token is a "
                    "login of its own and cannot be API credit"
                )
            parent_num, parent_email, parent_org = self.resolve_account(credit_for)
            if self._account_kind(parent_num) == "api_key":
                raise ValidationError(
                    f"Account {parent_num} ({parent_email}) is an API key; link "
                    "to a subscription account instead"
                )
            parent = (parent_email, parent_org)

        self._commit_add_account_from_token(
            token, email, slot, assume_yes, is_api_key, parent
        )
```

Change `_commit_add_account_from_token` to accept the link:

```python
    def _commit_add_account_from_token(
        self,
        token: str,
        email: str | None,
        slot: int | None,
        assume_yes: bool,
        is_api_key: bool,
        credit_for: tuple[str, str] | None = None,
    ) -> None:
```

In the refresh-in-place branch, after `self._write_account_config(account_num, email, config)`:

```python
                if credit_for is not None:
                    seq["accounts"][account_num]["creditFor"] = {
                        "email": credit_for[0], "organizationUuid": credit_for[1],
                    }
```

In the new-record branch, right after `record["kind"] = "api_key"`:

```python
            if credit_for is not None:
                record["creditFor"] = {
                    "email": credit_for[0], "organizationUuid": credit_for[1],
                }
```

- [ ] **Step 5: Print the orphan notice on parent removal**

In `src/openswap/engine/slots.py` `remove_account`, after `print(f"{accent('Removed')} Account-{account_num} ({email})")` and before `self._prune_mappings(...)`:

```python
        for num, record in sorted(
            (data or {}).get("accounts", {}).items(), key=lambda kv: int(kv[0])
        ):
            link = record.get("creditFor")
            if (
                isinstance(link, dict)
                and link.get("email") == email
                and (link.get("organizationUuid") or "") == confirmed_org
            ):
                print(dimmed(
                    f"  Slot {num} (API credit for {email}) is now a standalone key."
                ))
```

`data` here is the post-removal roster read inside the mutation guard; the key record is left untouched so the link resolves again when the parent returns.

- [ ] **Step 6: Carry the parent on the snapshot and order keys after parents**

In `src/openswap/models.py` `AccountSnapshot`, after `disabled: bool = False ...`:

```python
    credit_for: str = ""  # parent slot when this API key is a subscription account's credit
```

In `src/openswap/engine/snapshot.py` import at the top: `from openswap.engine.slots import order_keys_after_parents`.

In `_build_accounts_info`, replace `for num in data.get("sequence", []):` with:

```python
        sequence = list(data.get("sequence", []))
        parent_of = {str(n): self._credit_parent_in(data, str(n)) for n in sequence}
        for num in order_keys_after_parents(sequence, parent_of):
```

In `accounts_snapshot`, add to the `AccountSnapshot(...)` constructor call:

```python
                    credit_for=self._credit_parent_in(seq_data, n) or "",
```

- [ ] **Step 7: Run the tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_api_key_accounts.py -k "CreditLink or order_keys" -q`
Expected: PASS.

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: all passed.

- [ ] **Step 8: Commit**

```bash
git add src/openswap/engine/slots.py src/openswap/engine/switch.py src/openswap/engine/snapshot.py src/openswap/engine/protocol.py src/openswap/codex/engine.py src/openswap/models.py tests/test_api_key_accounts.py
git commit -m "Link an API key slot to the subscription account it is credit for"
```

---

### Task 2: Engine eligibility, key ordering, and credit-return

**Files:**
- Modify: `src/openswap/autoswitch.py:884-895` (active-key branch), `:969` (cooldown gate), `:975-995` (candidate census), `:1020` (`consume_first`), `:1142-1146` (last resort), `:1760` (`_rank_candidates` landing gate), `:2058` (`_perform` cooldown)
- Test: `tests/test_autoswitch.py` (`TestApiKeyAccounts`, line 1740)

**Interfaces:**
- Consumes: `switcher.credit_parent_for(num) -> str | None` (Task 1), `switcher.account_kind_for(num)`, `switcher.switchable_account_numbers()`.
- Produces: trigger string `"credit-return"` on `SwitchEvent.trigger`; `NoSwitchEvent(reason="on-api-credit", detail="no subscription account below 90%")`; private helpers `AutoSwitchEngine._key_eligible(num, settings) -> bool` and `AutoSwitchEngine._order_keys(keys, *, leaving) -> list[str]`.

- [ ] **Step 1: Write the failing engine tests**

Add inside `class TestApiKeyAccounts` in `tests/test_autoswitch.py` (keep the existing four tests):

```python
    def _link(self, harness, key_num: int, parent_num: int) -> None:
        data = harness.switcher._get_sequence_data()
        parent = data["accounts"][str(parent_num)]
        data["accounts"][str(key_num)]["creditFor"] = {
            "email": parent["email"],
            "organizationUuid": parent.get("organizationUuid", "") or "",
        }
        harness.switcher._write_json(harness.switcher.sequence_file, data)

    def _no_switch_reasons(self, h) -> list[str]:
        return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]

    def test_linked_key_eligible_without_flag(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        self._link(h, 2, 1)
        outcome = h.tick_with_usage({"1": _usage(100), "2": "api key"})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"

    def test_unlinked_key_still_excluded_without_flag(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        assert h.tick_with_usage({"1": _usage(100), "2": "api key"}) is TickOutcome.BLOCKED
        assert h.active_number() == 1

    def test_subscription_with_room_beats_linked_key(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        self._link(h, 2, 1)
        outcome = h.tick_with_usage({"1": _usage(95), "2": "api key", "3": _usage(10)})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_left_accounts_key_wins_then_linked_then_unlinked(self, temp_home):
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.seed(1, "a@example.com")
        h.seed(2, "loose@token.local")
        h.seed(3, "b@example.com")
        h.seed(4, "bkey@token.local")
        h.seed(5, "akey@token.local")
        h.make_live("a@example.com", 1)
        for n in (2, 4, 5):
            self._mark_api_key(h, n)
        self._link(h, 4, 3)
        self._link(h, 5, 1)
        usage = {"1": _usage(100), "2": "api key", "3": _usage(100), "4": "api key", "5": "api key"}
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.active_number() == 5
        assert h.engine._order_keys(["2", "4", "5"], leaving="1") == ["5", "4", "2"]

    def test_two_keys_same_parent_lower_slot_first(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "k2@token.local")
        h.seed(3, "k3@token.local")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        self._mark_api_key(h, 3)
        self._link(h, 2, 1)
        self._link(h, 3, 1)
        assert h.tick_with_usage({"1": _usage(100), "2": "api key", "3": "api key"}) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_linked_key_eligible_when_parent_disabled(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("c@example.com", 3)
        self._mark_api_key(h, 2)
        self._link(h, 2, 1)
        h.switcher.set_account_disabled("1", True)
        assert h.tick_with_usage({"1": _usage(10), "2": "api key", "3": _usage(100)}) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_credit_return_to_best_healthy_subscription(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("key@token.local", 2)
        self._mark_api_key(h, 2)
        self._link(h, 2, 1)
        outcome = h.tick_with_usage({"1": _usage(50), "2": "api key", "3": _usage(20)})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "credit-return"

    def test_credit_return_holds_when_every_subscription_is_at_threshold(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("key@token.local", 2)
        self._mark_api_key(h, 2)
        self._link(h, 2, 1)
        for _ in range(10):
            outcome = h.tick_with_usage({"1": _usage(92), "2": "api key", "3": _usage(90)})
            h.clock.advance(60)
            assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert set(self._no_switch_reasons(h)) == {"on-api-credit"}
        assert h.events[-1].detail == "no subscription account below 90%"
        assert h.engine._unhealthy_ticks == 0

    def test_credit_return_respects_cooldown(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.make_live("key@token.local", 2)
        self._mark_api_key(h, 2)
        self._link(h, 2, 1)
        record_manual_switch(h.switcher.backup_dir, now=h.clock.now)
        assert h.tick_with_usage({"1": _usage(10), "2": "api key"}) is TickOutcome.NO_ACTION
        assert self._no_switch_reasons(h) == ["cooldown"]
        h.clock.advance(h.settings.cooldown_seconds + 1)
        assert h.tick_with_usage({"1": _usage(10), "2": "api key"}) is TickOutcome.SWITCHED
        assert h.active_number() == 1

    def test_credit_return_skips_quarantined_parent(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.make_live("key@token.local", 2)
        self._mark_api_key(h, 2)
        self._link(h, 2, 1)
        h.engine._quarantine("1", "a@example.com", "invalid_grant")
        h.events.clear()
        assert h.tick_with_usage({"1": _usage(10), "2": "api key"}) is TickOutcome.NO_ACTION
        assert self._no_switch_reasons(h) == ["on-api-credit"]
        assert h.active_number() == 2

    def test_credit_return_never_lands_on_another_key(self, temp_home):
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "other@token.local")
        h.make_live("key@token.local", 2)
        self._mark_api_key(h, 2)
        self._mark_api_key(h, 3)
        self._link(h, 2, 1)
        assert h.tick_with_usage({"1": _usage(95), "2": "api key", "3": "api key"}) is TickOutcome.NO_ACTION
        assert h.active_number() == 2

    def test_consume_first_never_targets_linked_key(self, temp_home):
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        self._link(h, 2, 1)
        assert h.tick_with_usage({"1": _usage(10), "2": "api key"}) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_autoswitch.py -k TestApiKeyAccounts -q`
Expected: the new tests FAIL (linked key not eligible, no `_order_keys`, credit-return lands via `active-usage-unknown`/`failover` or not at all); the four existing tests still PASS.

- [ ] **Step 3: Add the eligibility and ordering helpers**

In `src/openswap/autoswitch.py`, inside `AutoSwitchEngine`, near `_in_cooldown` (line 2112):

```python
    def _key_eligible(self, num: str, settings: AutoSwitchSettings) -> bool:
        """A linked key is eligible by the link; an unlinked one needs the global flag."""
        return (
            self.switcher.credit_parent_for(num) is not None
            or settings.include_api_key_accounts
        )

    def _order_keys(self, keys: list[str], *, leaving: str) -> list[str]:
        """The left account's own key first, then other linked keys, then unlinked."""
        def rank(num: str) -> tuple[int, int]:
            parent = self.switcher.credit_parent_for(num)
            if parent == leaving:
                group = 0
            elif parent is not None:
                group = 1
            else:
                group = 2
            return (group, int(num))

        return sorted(keys, key=rank)
```

- [ ] **Step 4: Replace the active-key branch with the credit-return trigger**

Replace lines 884-895 (the `if self.switcher.account_kind_for(current) == "api_key" and not settings.include_api_key_accounts:` block) and change the following `if active_headroom is not None:` into an `elif`, so the three branches read:

```python
        active_headroom = headroom.get(current)
        if self.switcher.account_kind_for(current) == "api_key":
            if not self._key_eligible(current, settings):
                self._emit(
                    NoSwitchEvent(
                        reason="active-api-key",
                        detail="API-key accounts have no quota to watch",
                    )
                )
                return TickOutcome.NO_ACTION
            # On API credit: a key has no window to read, so unhealthy
            # counting never applies. Leave only for a subscription below
            # the threshold (`_rank_candidates`, trigger "credit-return").
            self._unhealthy_ticks = 0
            self._idle_hold_since = None
            trigger = "credit-return"
        elif active_headroom is not None:
            self._unhealthy_ticks = 0
            self._idle_hold_since = None
            utilization = 100.0 - active_headroom
            ...  # unchanged
        else:
            ...  # unchanged
```

The existing `active_headroom = headroom.get(current)` line already sits just above; keep one assignment.

- [ ] **Step 5: Gate cooldown, census, strategy, and last resort on the new trigger**

Line 969:

```python
        if trigger in ("proactive", "consume-first", "credit-return") and self._in_cooldown(state):
```

Candidate census (replace the `api_key_candidates = (...)` expression):

```python
        api_key_candidates = (
            []
            if trigger == "credit-return"
            else self._order_keys(
                [
                    n for n in candidates
                    if self.switcher.account_kind_for(n) == "api_key"
                    and self._key_eligible(n, settings)
                ],
                leaving=current,
            )
        )
```

Directly after that census, the existing `if not oauth_candidates and not api_key_candidates:` block returns `BLOCKED` with `no-candidates`; a key that is active with no subscription peer at all must hold on credit instead. Change it to:

```python
        if not oauth_candidates and not api_key_candidates:
            if trigger == "credit-return":
                return self._hold_on_credit(settings)
            # Won't change until the user adds/recovers an account — no point
            # re-polling at full cadence.
            self._blocked_wait_long = True
            self._emit(NoSwitchEvent(reason="no-candidates"))
            return TickOutcome.BLOCKED
```

and add the helper next to `_key_eligible`:

```python
    def _hold_on_credit(self, settings: AutoSwitchSettings) -> TickOutcome:
        """Staying on paid credit is a correct outcome, not a block: a
        subscription window can roll over on any tick."""
        self._emit(
            NoSwitchEvent(
                reason="on-api-credit",
                detail=(
                    "no subscription account below "
                    f"{pct_label(settings.threshold)}%"
                ),
            )
        )
        return TickOutcome.NO_ACTION
```

Line 1020:

```python
        consume_first = (
            settings.strategy in CONSUME_STRATEGIES and trigger != "credit-return"
        )
```

After the last-resort block (`if not ordered and api_key_candidates and trigger != "consume-first": ordered = api_key_candidates`), add:

```python
        if not ordered and trigger == "credit-return":
            return self._hold_on_credit(settings)
```

In `_rank_candidates` (line ~1760), widen the healthy-landing gate:

```python
            if trigger in ("proactive", "consume-first", "credit-return"):
```

`all_above` is already `False` whenever `active_headroom` is `None` (`_every_account_above_threshold` requires a known active headroom), so the soonest-back escape never engages for credit-return; the `elif active_headroom is not None` hysteresis is skipped for the same reason and the sort key falls through to `(-h,)`.

In `_perform` (line 2058):

```python
            if trigger in ("proactive", "consume-first", "credit-return") and self._in_cooldown(state):
```

- [ ] **Step 6: Run the tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_autoswitch.py -k TestApiKeyAccounts -q`
Expected: PASS.

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: all passed. If `tests/test_json_output.py` or a CLI test enumerates `NoSwitchEvent` reasons or `SwitchEvent` triggers, add `on-api-credit` and `credit-return` to that list.

- [ ] **Step 7: Commit**

```bash
git add src/openswap/autoswitch.py tests/test_autoswitch.py
git commit -m "Make linked API keys the last resort and return only to a healthy subscription"
```

---

### Task 3: CLI `--for`, list marker, JSON field, config help

**Files:**
- Modify: `src/openswap/cli.py:1617-1635` (argument block), `:1808-1812` (modifier checks), `:1863-1868` (dispatch), `:1527,1567` (help text)
- Modify: `src/openswap/engine/snapshot.py:1763-1772` (`list_accounts` markers), `:1685-1703` (`_build_list_payload`)
- Modify: `src/openswap/json_output.py:217-258` (`account_row`)
- Modify: `src/openswap/settings.py:213-216`
- Test: `tests/test_cli.py:808-862`, `tests/test_api_key_accounts.py`, `tests/test_json_output.py`

**Interfaces:**
- Consumes: `add_account_from_token(..., credit_for=)`, `credit_parent_for`, `_credit_parent_in` (Task 1).
- Produces: `openswap add-token TOKEN --for NUM|EMAIL|ALIAS`; `account_row(..., credit_for: int | None = None)` adding `"creditFor": int` only when linked.

- [ ] **Step 1: Write the failing CLI and output tests**

In `tests/test_cli.py`, update the two existing dispatch assertions to include the new keyword:

```python
        mock_add.assert_called_once_with(
            token="mytoken", email="u@example.com", slot=None, credit_for=None
        )
```

and

```python
        mock_add.assert_called_once_with(
            token="tok", email="u@example.com", slot=3, credit_for=None
        )
```

Then add after `test_add_token_with_slot`:

```python
    def test_add_token_for_dispatches_credit_for(self, temp_home: Path, capsys):
        from openswap.switcher import ClaudeAccountSwitcher

        with patch.object(
            sys, "argv", ["openswap", "add-token", "sk-ant-api03-x", "--for", "work"],
        ), patch.object(
            ClaudeAccountSwitcher, "add_account_from_token"
        ) as mock_add:
            cli.main()

        mock_add.assert_called_once_with(
            token="sk-ant-api03-x", email=None, slot=None, credit_for="work"
        )

    def test_for_without_add_token_errors(self, capsys):
        with patch.object(sys, "argv", ["openswap", "list", "--for", "1"]):
            with pytest.raises(SystemExit):
                cli.main()
        assert "--for can only be used with 'add-token'" in capsys.readouterr().err
```

In `tests/test_api_key_accounts.py` `TestCreditLink`, add:

```python
    def test_list_shows_credit_marker_and_json_field(self, temp_home: Path, capsys):
        s = _linux_switcher()
        _seed_oauth(s, 1, "a@example.com")
        s.add_account_from_token(API_KEY, credit_for="1")
        capsys.readouterr()
        s.list_accounts(fetch=set())
        out = capsys.readouterr().out
        assert "(API credit for a@example.com)" in out
        payload = s.list_accounts(json_output=True, fetch=set())
        rows = {row["number"]: row for row in payload["accounts"]}
        assert rows[2]["creditFor"] == 1
        assert "creditFor" not in rows[1]
        assert [row["number"] for row in payload["accounts"]] == [1, 2]
```

In `tests/test_json_output.py`, add:

```python
def test_account_row_credit_for_is_additive():
    from openswap.json_output import account_row
    assert "creditFor" not in account_row(1, "a@x", "", "", False, None)
    assert account_row(2, "k@x", "", "", False, "api key", credit_for=1)["creditFor"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "add_token or for_without" tests/test_json_output.py -k credit tests/test_api_key_accounts.py -k list_shows -q`
Expected: FAIL (`unrecognized arguments: --for`, missing `creditFor`).

- [ ] **Step 3: Add the argument, the checks, and the dispatch**

In `src/openswap/cli.py`, after the `--email` argument:

```python
    parser.add_argument(
        "--for",
        dest="credit_for",
        metavar="NUM|EMAIL|ALIAS",
        help=(
            "With 'add-token': link this API key to a subscription account as "
            "its API credit. Auto-switch uses it when every subscription is out "
            "of quota and returns to a subscription once one has room."
        ),
    )
```

After the `--email` modifier check:

```python
    if args.credit_for is not None and args.add_token is None:
        parser.error("--for can only be used with 'add-token'")
```

Dispatch:

```python
        elif args.add_token is not None:
            switcher.add_account_from_token(
                token=args.add_token,
                email=args.email,
                slot=args.slot,
                credit_for=args.credit_for,
            )
```

Help text: at line 1527 change the line to `%(prog)s add-token [TOKEN|-] [--for ID]  register an API key or setup-token`, and after the example at line 1567 add `  %(prog)s add-token sk-ant-api03-... --for 2   # API credit for account 2`.

- [ ] **Step 4: Show the link in list and JSON**

In `src/openswap/engine/snapshot.py` `list_accounts`, after the `(disabled)` marker:

```python
            parent = self._credit_parent_in(seq_data, str(num))
            if parent is not None:
                markers += f" {muted(f'(API credit for {self.account_email(parent)})')}"
```

In `_build_list_payload`, pass the link:

```python
            parent = self._credit_parent_in(seq_data, str(num))
            accounts.append(
                account_row(
                    num, email, org_name, org_uuid, is_active,
                    entry.decision_value(),
                    usage_fetched_at=entry.fetched_at,
                    usage_age_s=entry.age_s,
                    last_good_usage=entry.last_good,
                    alias=alias,
                    disabled=self._disabled_from_data(seq_data, str(num)),
                    credit_for=int(parent) if parent is not None else None,
                )
            )
```

In `src/openswap/json_output.py` `account_row`, add the keyword `credit_for: int | None = None,` after `disabled`, and after the `if disabled:` block:

```python
    # Additive field: the parent slot when this API key is a subscription
    # account's credit (plan 019).
    if credit_for is not None:
        row["creditFor"] = credit_for
```

In `src/openswap/settings.py`, change the help string:

```python
            help=(
                "Allow rotating onto API-key accounts that are not linked to a "
                "subscription account (bill per token); keys added with "
                "'add-token --for' are always eligible as a last resort"
            ),
```

- [ ] **Step 5: Run the tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_cli.py tests/test_json_output.py tests/test_api_key_accounts.py tests/test_settings.py tests/test_config_cli.py -q`
Expected: PASS.

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/openswap/cli.py src/openswap/engine/snapshot.py src/openswap/json_output.py src/openswap/settings.py tests/test_cli.py tests/test_api_key_accounts.py tests/test_json_output.py
git commit -m "Add add-token --for and show a linked key as API credit in list output"
```

---

### Task 4: Extra cards and hold line

**Files:**
- Modify: `src/openswap/menubar_display.py:2725-2810` (`_adapt_snapshot` and the empty-snapshot dict), `:2058-2095` (`panel_accounts` title/subtitle), `:2370` (`hold_line_from_event`)
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: `AccountSnapshot.credit_for` (Task 1); `NoSwitchEvent(reason="on-api-credit", detail=...)` (Task 2).
- Produces: snapshot dict key `"credit_for": {num: parent_num}`; card keys `"credit_for": str`; card title `"API credit"` with subtitle `"for a@example.com"`.

- [ ] **Step 1: Write the failing extra tests**

Add to `tests/test_menubar.py` near `test_panel_accounts_prefers_alias_and_keeps_note`:

```python
def test_panel_accounts_titles_linked_key_as_api_credit():
    snap = {
        "accounts": [
            (1, "a@x.com", True, _USAGE, _USAGE, "", "", False, None),
            (2, "api-key-2@token.local", False, "api key", None, "", "", False, None),
            (3, "api-key-3@token.local", False, "api key", None, "", "", False, None),
        ],
        "kinds": {"1": "oauth", "2": "api_key", "3": "api_key"},
        "credit_for": {"2": "1"},
    }
    cards = menubar.panel_accounts(snap)
    assert cards[1]["title"] == "API credit"
    assert cards[1]["subtitle"] == "for a@x.com"
    assert cards[1]["credit_for"] == "1"
    assert cards[1]["api_key"] is True
    assert cards[2]["title"] == "personal"
    assert cards[2]["subtitle"] == "api-key-3@token.local"
    assert cards[2]["credit_for"] == ""


def test_adapt_snapshot_carries_credit_for():
    from types import SimpleNamespace
    from openswap.usage_store import UsageEntry

    def acc(number, email, kind, credit_for=""):
        return SimpleNamespace(
            number=number, email=email, is_active=False, usage=UsageEntry(),
            alias="", org_name="", org_uuid="", disabled=False, kind=kind,
            credit_for=credit_for,
        )

    snap = SimpleNamespace(accounts=[acc("1", "a@x.com", "oauth"), acc("2", "k@x", "api_key", "1")])
    adapted = menubar._adapt_snapshot(snap)
    assert adapted["credit_for"] == {"1": "", "2": "1"}


def test_hold_line_on_api_credit():
    assert menubar.hold_line_from_event(
        NoSwitchEvent(reason="on-api-credit", detail="no subscription account below 90%"),
        active_title="API credit", strategy="best",
    ) == "Holding on API credit: no subscription account below 90%."
    assert menubar.hold_line_from_event(
        NoSwitchEvent(reason="on-api-credit"), active_title="API credit", strategy="best",
    ) == "Holding on API credit."
```

`_adapt_snapshot` is re-exported by `openswap.menubar`; if the existing tests reach it through another name, use that name.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_menubar.py -k "api_credit or credit_for" -q`
Expected: FAIL (title is `personal`, no `credit_for` key, hold line `None`).

- [ ] **Step 3: Carry the map, title the card, render the hold line**

In `src/openswap/menubar_display.py` `_adapt_snapshot`: add `credit_for: dict[str, str] = {}` next to `kinds`, inside the Claude loop add `credit_for[str(acc.number)] = getattr(acc, "credit_for", "") or ""`, and add `"credit_for": credit_for,` to the returned dict. Add `"credit_for": {},` to the empty-snapshot dict at line 2732 next to `"kinds": {}`.

In `panel_accounts`, replace

```python
        title, subtitle = account_card_names(email, alias, org_name)
```

with

```python
        title, subtitle = account_card_names(email, alias, org_name)
        credit_parent = str((snapshot.get("credit_for") or {}).get(str(num)) or "")
        if credit_parent:
            parent_email = next(
                (str(r[1]) for r in snapshot.get("accounts") or []
                 if str(r[0]) == credit_parent),
                "",
            )
            title = "API credit"
            subtitle = f"for {parent_email}" if parent_email else ""
```

and add `"credit_for": credit_parent,` to the card dict after `"api_key": is_api_key,`.

In `hold_line_from_event`, before `if reason == "active-api-key":`:

```python
    if reason == "on-api-credit":
        return f"Holding on API credit: {detail}." if detail else "Holding on API credit."
```

- [ ] **Step 4: Run the tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_menubar.py tests/test_menubar_tabs.py tests/test_widget_snapshot.py -q`
Expected: PASS.

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/openswap/menubar_display.py tests/test_menubar.py
git commit -m "Show a linked key as API credit in the popover and name the credit hold"
```

---

### Task 5: Add from token page

**Files:**
- Modify: `src/openswap/menubar_display.py:250-251` (page constants), new helpers next to `login_panel_state` (line 2179)
- Modify: `src/openswap/menubar.py:1365-1395` (panel kwargs), `:2566-2585` (`on_add_token`), new `_add_token`
- Modify: `src/openswap/menubar_panel.py:830-900` (`__init__`), `:1152-1157` (next to `show_login`), `:1290-1294` (`_build` dispatch), new `_build_token_page`
- Modify: `docs/menubar.md` (More paragraph)
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: `add_account_from_token(token, email, slot, credit_for=)` (Task 1), `credentials.looks_like_api_key`, `account_card_names`, snapshot keys `accounts` and `kinds`.
- Produces:
  - `menubar_display.TOKEN_PAGE = "token"`, `SETUP_TOKEN_PREFIX = "sk-ant-oat"`
  - `menubar_display.token_entry_state(text: str, accounts: list, kinds: dict | None = None) -> dict` with keys `kind`, `line`, `show_parent`, `choices` (list of `(value, label)`), `can_add`
  - `menubar_display.sanitize_token_error(message: str) -> str`
  - `MenuBarApp._add_token(token: str, label: str, credit_for: str) -> str | None` (error copy, or `None` on success)
  - `MenuBarPanel(on_add_token=...)`, `MenuBarPanel.show_token_page()`

- [ ] **Step 1: Write the failing pure-model and harness tests**

Add to `tests/test_menubar.py`:

```python
_TOKEN_ROWS = [
    (1, "a@x.com", True, _USAGE, _USAGE, "work", "", False, None),
    (2, "b@x.com", False, _USAGE, _USAGE, "", "Ads Online", False, None),
    (3, "api-key-3@token.local", False, "api key", None, "", "", False, None),
    ("codex:1", "c@x.com", False, _USAGE, _USAGE, "", "", False, None),
]
_TOKEN_KINDS = {"1": "oauth", "2": "oauth", "3": "api_key", "codex:1": "oauth"}


def test_token_page_constant():
    assert menubar.TOKEN_PAGE == "token"


def test_token_entry_state_empty():
    state = menubar.token_entry_state("", _TOKEN_ROWS, _TOKEN_KINDS)
    assert state["kind"] is None
    assert state["line"] == ""
    assert state["show_parent"] is False
    assert state["can_add"] is False


def test_token_entry_state_api_key_offers_subscription_parents_only():
    state = menubar.token_entry_state("sk-ant-api03-abc", _TOKEN_ROWS, _TOKEN_KINDS)
    assert state["kind"] == "api_key"
    assert state["line"] == "API key"
    assert state["show_parent"] is True
    assert state["can_add"] is True
    assert state["choices"] == [
        ("", "None (standalone)"),
        ("1", "work · a@x.com"),
        ("2", "Ads Online · b@x.com"),
    ]


def test_token_entry_state_setup_token_hides_parent():
    state = menubar.token_entry_state("sk-ant-oat01-abc", _TOKEN_ROWS, _TOKEN_KINDS)
    assert state["kind"] == "setup_token"
    assert state["line"] == "Setup token"
    assert state["show_parent"] is False
    assert state["can_add"] is True


def test_token_entry_state_rejects_garbage():
    state = menubar.token_entry_state("hunter2", _TOKEN_ROWS, _TOKEN_KINDS)
    assert state["kind"] is None
    assert state["line"] == "Not a Claude token"
    assert state["can_add"] is False


def test_token_entry_state_strips_whitespace():
    state = menubar.token_entry_state("  sk-ant-api03-abc\n", _TOKEN_ROWS, _TOKEN_KINDS)
    assert state["kind"] == "api_key"
    assert state["can_add"] is True


def test_sanitize_token_error_strips_secrets_and_urls():
    assert menubar.sanitize_token_error(
        "Bad key sk-ant-api03-abcDEF_123 see https://x.test/a?b=c"
    ) == "Bad key see"
    assert menubar.sanitize_token_error("x" * 300).endswith("…")
    assert len(menubar.sanitize_token_error("x" * 300)) == 181


def test_add_token_returns_none_on_success_and_copy_on_failure():
    from types import SimpleNamespace
    from tests.menubar_harness import extract_class
    from openswap.exceptions import ValidationError

    scope = {
        "ClaudeSwitchError": menubar.ClaudeSwitchError,
        "sanitize_token_error": menubar.sanitize_token_error,
    }
    app_type = extract_class(menubar.__file__, "MenuBarApp", {"_add_token"}, scope)
    app = app_type()
    calls = []
    refreshed = []
    app.switcher = SimpleNamespace(
        add_account_from_token=lambda **kw: calls.append(kw)
    )
    app.refresh_async = lambda: refreshed.append(True)
    assert app._add_token("sk-ant-api03-abc", "", "1") is None
    assert calls == [{"token": "sk-ant-api03-abc", "email": None, "slot": None, "credit_for": "1"}]
    assert refreshed == [True]

    def boom(**kw):
        raise ValidationError("rejected sk-ant-api03-abc for https://x.test")

    app.switcher = SimpleNamespace(add_account_from_token=boom)
    assert app._add_token("sk-ant-api03-abc", "me", "") == "rejected for"
    assert refreshed == [True]
```

`menubar.ClaudeSwitchError` is the name `menubar.py` imports at line 18 from `openswap.exceptions`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_menubar.py -k "token" -q`
Expected: FAIL (`AttributeError: TOKEN_PAGE`, `token_entry_state`, `sanitize_token_error`, `_add_token`).

- [ ] **Step 3: Add the pure model and the sanitizer**

In `src/openswap/menubar_display.py`, next to `SETTINGS_PAGE`:

```python
TOKEN_PAGE = "token"
SETUP_TOKEN_PREFIX = "sk-ant-oat"
```

Add `from openswap.credentials import looks_like_api_key` to the imports if it is not already there, then next to `login_panel_state`:

```python
_SECRET_RE = re.compile(r"https?://\S+|\bsk-ant-[A-Za-z0-9_-]+|\b(?:sk|auth)_[A-Za-z0-9_-]+")


def sanitize_token_error(message: str) -> str:
    """Engine error copy for the token page: no URLs, no token-shaped text, one line."""
    text = str(message or "").replace("\n", " ")
    text = _SECRET_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if len(text) > 180:
        text = text[:180] + "…"
    return text


def token_entry_state(text: str, accounts: list, kinds: dict | None = None) -> dict:
    """Pure model for the Add-from-token page.

    ``accounts`` are the snapshot rows; ``kinds`` the snapshot kinds map. The
    page is stricter than the engine: only ``sk-ant-api`` and ``sk-ant-oat``
    prefixes are accepted, so a stray paste cannot be stored as a login.
    """
    value = (text or "").strip()
    if not value:
        kind, line = None, ""
    elif looks_like_api_key(value):
        kind, line = "api_key", "API key"
    elif value.startswith(SETUP_TOKEN_PREFIX):
        kind, line = "setup_token", "Setup token"
    else:
        kind, line = None, "Not a Claude token"
    choices: list[tuple[str, str]] = [("", "None (standalone)")]
    for row in accounts or []:
        num, email, _active, _display, _last_good, alias, org_name, _disabled, _fetched = row
        if str(num).startswith("codex:") or (kinds or {}).get(str(num)) == "api_key":
            continue
        title, _subtitle = account_card_names(email, alias, org_name)
        choices.append((str(num), f"{title} · {email}"))
    return {
        "kind": kind,
        "line": line,
        "show_parent": kind == "api_key",
        "choices": choices,
        "can_add": kind is not None,
    }
```

`openswap.menubar` does `from openswap.menubar_display import *` (line 19), so the new names are visible there as long as `menubar_display.py` has no `__all__` that excludes them (it has none today; if one exists when you get there, add the three names to it).

- [ ] **Step 4: Add `_add_token` to the extra and route the menu item to the page**

In `src/openswap/menubar.py`, replace `on_add_token`:

```python
        def on_add_token(self, _sender):
            if self._panel is None:
                self._add_token_prompts()
                return
            self._panel.show_token_page()
            if not self._panel.is_shown():
                self._panel.toggle()

        def _add_token_prompts(self):
            """Fallback when the popover could not attach: the old two prompts."""
            email_resp = self._prompt(
                title="Add account from token",
                message="Email label (optional; leave blank to auto-name):",
                ok="Next", cancel="Cancel",
            )
            if email_resp.clicked != 1:
                return
            email = email_resp.text.strip() or None
            token_resp = self._prompt(
                title="Add account from token",
                message="API key (sk-ant-api…) or setup token (sk-ant-oat01-…):",
                ok="Add", cancel="Cancel",
                secure=True,
            )
            if token_resp.clicked != 1 or not token_resp.text.strip():
                return
            if self._guard(lambda: self.switcher.add_account_from_token(
                token=token_resp.text.strip(), email=email, slot=None,
            )):
                self.refresh_async()

        def _add_token(self, token, label, credit_for):
            """Token page submit. Returns sanitized error copy, or None on success."""
            try:
                self.switcher.add_account_from_token(
                    token=token, email=label or None, slot=None,
                    credit_for=credit_for or None,
                )
            except ClaudeSwitchError as e:
                return sanitize_token_error(str(e))
            self.refresh_async()
            return None
```

Add `on_add_token=self._add_token,` to the `MenuBarPanel(...)` constructor call.

- [ ] **Step 5: Build the page in the panel**

In `src/openswap/menubar_panel.py`:

Import `TOKEN_PAGE`, `token_entry_state` from `openswap.menubar_display` alongside `MAIN_PAGE`/`SETTINGS_PAGE`; add `NSSecureTextField` to the `AppKit` import list; add `from Foundation import NSObject` if `NSObject` is not already imported.

Add a text-change forwarder next to `_Trampoline`:

```python
class _TextWatcher(NSObject):
    """NSTextField delegate that forwards edits to a Python callable."""

    def initWithCallback_(self, callback):
        self = objc.super(_TextWatcher, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def controlTextDidChange_(self, _notification):
        self._callback()
```

In `__init__`, add the kwarg `on_add_token=None` and the attributes:

```python
        self._on_add_token = on_add_token
        self._token_error = ""
        self._token_watcher = None
```

Next to `show_login`:

```python
    def show_token_page(self) -> None:
        """Open the Add-from-token page in the current popover."""
        self._selected_provider = "claude"
        self._token_error = ""
        self._page = TOKEN_PAGE
        self.reload()
```

In `_build`, before `snap = self._snapshot()`:

```python
        if self._page == TOKEN_PAGE:
            return self._build_token_page()
```

Add the builder after `_build_settings`:

```python
    def _build_token_page(self):
        pal = _colors()
        font_title = NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold)
        font_body = NSFont.systemFontOfSize_weight_(12, NSFontWeightRegular)
        font_small = NSFont.systemFontOfSize_weight_(11, NSFontWeightRegular)
        inner_w = PANEL_WIDTH - PAD * 2
        field_w = inner_w - 24
        snap = self._snapshot()
        rows = snap.get("accounts") or []
        kinds = snap.get("kinds") or {}
        state = token_entry_state("", rows, kinds)

        height = PAD + HEADER_H + 8 + 28 + 6 + 18 + 10 + 28 + 10 + 28 + 10 + 18 + 10 + 40 + PAD
        root = _RootView.alloc().initWithHover_(self._on_hover)
        root.setFrame_(NSMakeRect(0, 0, PANEL_WIDTH, height))
        root.setMaterial_(NSVisualEffectMaterialMenu)
        root.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        root.setState_(NSVisualEffectStateActive)

        title = _label("Add from token", font_title, pal["fg"], NSMakeRect(0, 0, 160, 20))
        title.sizeToFit()
        ts = title.frame().size
        back_f, title_f = settings_header_frames(
            PAD, (SETTINGS_BACK_W, SETTINGS_BACK_H), (ts.width, ts.height)
        )
        self._add_button(root, "Back", NSMakeRect(*back_f), self._show_main, font_small)
        title.setFrame_(NSMakeRect(*title_f))
        root.addSubview_(title)
        hairline = _FillView.alloc().initWithColor_(pal["hairline"])
        hairline.setFrame_(NSMakeRect(PAD, PAD + HEADER_H - 4, inner_w, 1))
        root.addSubview_(hairline)

        y = PAD + HEADER_H + 8
        token_field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(PAD + 12, y, field_w, 28))
        token_field.setPlaceholderString_("API key (sk-ant-api…) or setup token (sk-ant-oat…)")
        token_field.setFont_(font_body)
        root.addSubview_(token_field)
        y += 28 + 6
        detect = _label("", font_small, pal["muted"], NSMakeRect(PAD + 12, y, field_w, 18))
        root.addSubview_(detect)
        y += 18 + 10

        parent_label = _label("Use as API credit for", font_small, pal["muted"], NSMakeRect(PAD + 12, y, field_w, 18))
        root.addSubview_(parent_label)
        y += 18 + 4
        popup = _PopupButton.alloc().initWithPanel_frame_(self, NSMakeRect(PAD + 12, y, field_w, 28))
        popup.setControlSize_(NSControlSizeSmall)
        popup.setFont_(font_body)
        popup.removeAllItems()
        for value, caption in state["choices"]:
            popup.addItemWithTitle_(caption)
            item = popup.lastItem()
            if item is not None:
                item.setRepresentedObject_(value)
        root.addSubview_(popup)
        y += 28 + 10

        label_field = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 12, y, field_w, 28))
        label_field.setPlaceholderString_("Label (optional)")
        label_field.setFont_(font_body)
        label_field.cell().setUsesSingleLineMode_(True)
        root.addSubview_(label_field)
        y += 28 + 10

        error = _label(self._token_error, font_small, pal["muted"], NSMakeRect(PAD + 12, y, field_w, 18))
        root.addSubview_(error)
        y += 18 + 10

        add = self._add_button(
            root, "Add", NSMakeRect(PAD + 12, y, field_w, 40),
            lambda _s: self._submit_token(token_field, label_field, popup, error),
            font_body,
        )
        add.setEnabled_(False)
        add.setKeyEquivalent_("\r")

        def refresh():
            current = token_entry_state(token_field.stringValue(), rows, kinds)
            detect.setStringValue_(current["line"])
            parent_label.setHidden_(not current["show_parent"])
            popup.setHidden_(not current["show_parent"])
            add.setEnabled_(current["can_add"] and self._on_add_token is not None)

        self._token_watcher = _TextWatcher.alloc().initWithCallback_(refresh)
        token_field.setDelegate_(self._token_watcher)
        refresh()
        return root

    def _submit_token(self, token_field, label_field, popup, error_label):
        snap = self._snapshot()
        state = token_entry_state(
            token_field.stringValue(), snap.get("accounts") or [], snap.get("kinds") or {}
        )
        if not state["can_add"] or self._on_add_token is None:
            return
        credit_for = ""
        if state["show_parent"]:
            item = popup.selectedItem()
            credit_for = str(item.representedObject() or "") if item is not None else ""
        error = self._on_add_token(
            token_field.stringValue().strip(),
            label_field.stringValue().strip(),
            credit_for,
        )
        if error:
            self._token_error = error
            error_label.setStringValue_(error)
            return
        self._token_error = ""
        self._show_main()
```

The token never enters `self._token_error`, the snapshot, or any log; it lives in the field and the engine call only. Hidden controls keep their frame, so the page height does not change with the token kind.

- [ ] **Step 6: Run the tests, then the full suite, then try the page by hand**

Run: `uv run pytest tests/test_menubar.py -q`
Expected: PASS.

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: all passed.

Manual check (the page is AppKit and has no automated test): restart the extra per `docs/hacking.md`, open More → Add account → From API key or setup token…, and confirm: the field masks input; typing `sk-ant-api03-x` shows "API key" and the popup; typing `sk-ant-oat01-x` shows "Setup token" and hides the popup; `hunter2` shows "Not a Claude token" with Add disabled; Back returns to the cards; Esc closes the popover without adding. Do not submit a real key during this check.

- [ ] **Step 7: Document the page and commit**

In `docs/menubar.md`, in the paragraph that lists More's items, after "Add including ChatGPT sign-in" add: "From API key or setup token… opens the in-popover **Add from token** page (`TOKEN_PAGE`): a masked token field with live kind detection (`token_entry_state`), a **Use as API credit for** popup shown only for `sk-ant-api…` keys, an optional label, and inline sanitized engine errors (`sanitize_token_error`). The two old modal prompts remain only when the popover cannot attach." In the "Card title" sentence add: "A key linked with `add-token --for` is titled **API credit** with subtitle `for <parent email>` and sits directly under its parent."

```bash
git add src/openswap/menubar_display.py src/openswap/menubar.py src/openswap/menubar_panel.py docs/menubar.md tests/test_menubar.py
git commit -m "Add an in-popover page for entering an API key or setup token"
```

---

### Task 6: User docs, architecture note, plan status

**Files:**
- Modify: `README.md` (Quick start block, Commands table)
- Modify: `docs/architecture.md` (Product surface paragraph)
- Modify: `plans/019-api-credit-fallback.md` (Status), `plans/README.md` (019 row)

- [ ] **Step 1: README**

In the Quick start block, after the `openswap add-token sk-ant-api03-...` line add:

```
openswap add-token sk-ant-api03-... --for 1   # API credit for account 1: used when every subscription is out
```

In the Commands table, change the `openswap add-token` row to: `| `openswap add-token` | Save an API key or setup token; `--for NUM|EMAIL|ALIAS` links a key to a subscription account as its API credit |`.

- [ ] **Step 2: Architecture**

In `docs/architecture.md` "Product surface", replace the last sentence of the first paragraph ("They have no 5h/7d quota, so kickoff and autoswitch skip them unless `autoswitch.includeApiKeyAccounts` is on.") with:

"They have no 5h/7d quota, so kickoff skips them. A key linked to a subscription account (`add-token --for`, roster field `creditFor` holding the parent's `(email, organizationUuid)`) is the auto-switch engine's last resort when no subscription has room; an unlinked key needs `autoswitch.includeApiKeyAccounts`. While a key is active the engine leaves it only for a subscription below the threshold (trigger `credit-return`, hold reason `on-api-credit`). OpenSwap cannot see a revoked key or an exhausted balance (mar3co/openswap#97)."

- [ ] **Step 3: Plan status and commit**

In `plans/019-api-credit-fallback.md` set `Status: DONE. Implemented on <branch> from 1a6bfb5.` (keep the implementation-plan pointer) and in `plans/README.md` change the 019 row's status to `DONE`.

```bash
uv run pytest -q 2>&1 | tail -3
git add README.md docs/architecture.md plans/019-api-credit-fallback.md plans/README.md
git commit -m "Document API credit fallback"
```
