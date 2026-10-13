"""Frozen breakpoint drafts must never mutate a newer hold or message."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy import http
from mitmproxy.test import tflow

from ferret.core.mitm.addons import GatewayPolicy
from ferret.core.mitm.compose import ComposeAddon
from ferret.core.mitm.facade import MARKER_DEFAULT, MitmFacade
from ferret.core.mitm.held_edit import (
    HELD_EDIT_BODY_KEY,
    HELD_EDIT_EPOCH_KEY,
    HELD_EDIT_GUARD_KEY,
    HeldEditGuard,
    held_edit_body,
    held_edit_guard,
)
from ferret.core.mitm.intercept import RequestEdit, ResponseEdit
from tests.core.mitm.test_facade import _InlineRuntime


class HeldEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _InlineRuntime()
        self.facade = MitmFacade(self.runtime)  # ty: ignore[invalid-argument-type]
        self.flow = tflow.tflow()
        self.runtime.view.add([self.flow])
        self.runtime.master.intercept_state.arm(self.flow)

    def snapshot(self):
        return next(f for f in self.facade.intercepted_flows() if f.id == self.flow.id)

    def guard(self) -> HeldEditGuard:
        guard = held_edit_guard(self.snapshot())
        assert guard is not None
        return guard

    def edit(self) -> RequestEdit:
        return RequestEdit(
            method="POST",
            url=self.flow.request.url,
            headers=list(self.flow.request.headers.items(multi=True)),
            content=b"edited",
        )

    def assert_rejected_without_mutation(self, guard: HeldEditGuard) -> None:
        before = self.flow.get_state()
        with (
            patch.object(self.runtime.view, "update") as update,
            self.assertRaises(ValueError),
        ):
            self.facade.apply_request_edits(
                self.flow.id, self.edit(), guard=guard, release=True
            )
        self.assertEqual(self.flow.get_state(), before)
        update.assert_not_called()

    def test_snapshot_has_frozen_guard_and_decoded_body_without_live_copies(self):
        self.flow.request.content = b"line\r\nbody"
        self.flow.request.encode("gzip")
        wire = self.flow.request.raw_content
        snapshot = self.snapshot()
        guard = held_edit_guard(snapshot)
        source = held_edit_body(snapshot)
        assert guard is not None and source is not None
        self.assertEqual(guard.flow_id, self.flow.id)
        self.assertEqual(guard.phase, "request")
        self.assertEqual(guard.generation, self.runtime._generation)
        self.assertEqual(source.content, b"line\r\nbody")
        self.assertTrue(source.available)
        self.assertTrue(source.decode_ok)
        self.assertNotIn(HELD_EDIT_GUARD_KEY, self.flow.metadata)
        self.assertNotIn(HELD_EDIT_BODY_KEY, self.flow.metadata)
        self.assertEqual(self.flow.request.raw_content, wire)
        self.assertEqual(held_edit_guard(snapshot.copy()), guard)
        self.assertEqual(held_edit_body(snapshot.copy()), source)
        self.flow.request.content = b"new content"
        self.assertEqual(held_edit_body(snapshot), source)

    def test_missing_and_broken_bodies_are_reported_without_decode_errors(self):
        self.flow.request.raw_content = None
        source = held_edit_body(self.snapshot())
        assert source is not None
        self.assertFalse(source.available)
        self.flow.request.raw_content = b"not gzip"
        self.flow.request.headers["content-encoding"] = "gzip"
        source = held_edit_body(self.snapshot())
        assert source is not None
        self.assertTrue(source.available)
        self.assertFalse(source.decode_ok)

    def test_queue_refresh_marks_comments_and_timing_do_not_expire_the_draft(self):
        guard = self.guard()
        other = tflow.tflow()
        self.runtime.view.add([other])
        self.runtime.master.intercept_state.arm(other)
        self.facade.set_flow_marked(self.flow.id, MARKER_DEFAULT)
        self.facade.set_flow_comment(self.flow.id, "annotation")
        self.flow.request.timestamp_end = 9999.0
        self.assertEqual(self.guard(), guard)
        self.facade.apply_request_edits(self.flow.id, self.edit(), guard=guard)
        self.assertEqual(self.flow.request.raw_content, b"edited")

    def test_validation_mutation_update_and_release_share_one_runtime_call(self):
        guard = self.guard()
        observed = []

        def on_update(flow):
            observed.append((flow.request.raw_content, flow.intercepted))

        self.runtime.view.sig_view_update.connect(on_update)
        with patch.object(self.runtime, "call", wraps=self.runtime.call) as call:
            self.facade.apply_request_edits(
                self.flow.id, self.edit(), guard=guard, release=True
            )
        self.assertEqual(call.call_count, 1)
        self.assertEqual(observed, [(b"edited", True)])
        self.assertFalse(self.flow.intercepted)

    def test_runtime_restart_rejects_the_previous_generation(self):
        guard = self.guard()
        self.runtime._generation += 1
        self.assert_rejected_without_mutation(guard)

    def test_same_phase_reholds_expire_drafts_for_every_holder(self):
        for owner in ("intercept", "gateway", "native"):
            with self.subTest(owner=owner):
                self.facade.release_all_intercepted()
                self.hold(owner)
                guard = self.guard()
                self.facade.release_flows([self.flow.id])
                self.hold(owner)
                newer = self.guard()
                self.assertEqual(newer.source_version, guard.source_version)
                self.assertNotEqual(newer.edit_epoch, guard.edit_epoch)
                self.assert_rejected_without_mutation(guard)

    def hold(self, owner: str) -> None:
        if owner == "intercept":
            self.runtime.master.intercept_state.arm(self.flow)
        elif owner == "gateway":
            self.runtime.master.gateway.suspend(self.flow, GatewayPolicy.SUSPEND_OUT)
        else:
            self.flow.intercept()

    def test_request_draft_cannot_write_to_the_response_phase(self):
        guard = self.guard()
        self.facade.release_flows([self.flow.id])
        self.flow.response = http.Response.make(200, b"response")
        self.runtime.master.intercept_state.arm(self.flow)
        self.assert_rejected_without_mutation(guard)

    def test_released_target_is_rejected(self):
        guard = self.guard()
        self.facade.release_flows([self.flow.id])
        self.assert_rejected_without_mutation(guard)

    def test_missing_target_and_wrong_guard_id_are_rejected(self):
        guard = self.guard()
        self.assert_rejected_without_mutation(replace(guard, flow_id="other"))
        self.runtime.view.remove([self.flow])
        with self.assertRaises(ValueError):
            self.facade.apply_request_edits(self.flow.id, self.edit(), guard=guard)

    def test_source_changes_reject_without_discarding_the_hold_identity(self):
        for change in (
            lambda: setattr(self.flow.request, "method", "HEAD"),
            lambda: setattr(self.flow.request, "path", "/changed"),
            lambda: self.flow.request.headers.add("X-New", "value"),
            lambda: setattr(self.flow.request, "raw_content", b"new"),
            lambda: setattr(self.flow.request, "raw_content", None),
            lambda: setattr(
                self.flow.request, "trailers", http.Headers([(b"X", b"y")])
            ),
        ):
            guard = self.guard()
            change()
            newer = self.guard()
            self.assertEqual(newer.identity, guard.identity)
            self.assertNotEqual(newer.source_version, guard.source_version)
            self.assert_rejected_without_mutation(guard)

    def test_response_guard_covers_source_request_method(self):
        self.facade.release_flows([self.flow.id])
        self.flow.response = http.Response.make(200, b"response")
        self.runtime.master.intercept_state.arm(self.flow)
        guard = self.guard()
        self.flow.request.method = "HEAD"
        before = self.flow.get_state()
        with self.assertRaises(ValueError):
            self.facade.apply_response_edits(
                self.flow.id,
                ResponseEdit(201, [], b"changed"),
                guard=guard,
                release=True,
            )
        self.assertEqual(self.flow.get_state(), before)

    def test_response_edit_accepts_a_current_guard_and_releases(self):
        self.facade.release_flows([self.flow.id])
        self.flow.response = http.Response.make(200, b"response")
        self.runtime.master.intercept_state.arm(self.flow)
        self.facade.apply_response_edits(
            self.flow.id,
            ResponseEdit(201, [], b"changed"),
            guard=self.guard(),
            release=True,
        )
        self.assertFalse(self.flow.intercepted)
        self.assertEqual(self.flow.response.status_code, 201)
        self.assertEqual(self.flow.response.raw_content, b"changed")

    def test_invalid_candidate_does_not_update_or_release(self):
        guard = self.guard()
        before = self.flow.get_state()
        with (
            patch.object(self.runtime.view, "update") as update,
            self.assertRaises(ValueError),
        ):
            self.facade.apply_request_edits(
                self.flow.id,
                replace(self.edit(), method=""),
                guard=guard,
                release=True,
            )
        self.assertEqual(self.flow.get_state(), before)
        update.assert_not_called()

    def test_forget_and_compose_cancel_invalidate_the_epoch(self):
        epoch = self.guard().edit_epoch
        self.runtime.master.intercept_state.forget([self.flow.id])
        self.assertNotEqual(self.flow.metadata[HELD_EDIT_EPOCH_KEY], epoch)
        self.assertTrue(self.flow.intercepted)
        epoch = self.guard().edit_epoch
        addon = ComposeAddon(self.runtime.view)
        addon._cancel_flow(self.flow)
        self.assertNotEqual(self.flow.metadata[HELD_EDIT_EPOCH_KEY], epoch)
        self.assertFalse(self.flow.intercepted)

    def test_plain_snapshots_and_malformed_metadata_have_no_guard(self):
        plain = self.facade.all_http_flows()[0]
        self.assertIsNone(held_edit_guard(plain))
        self.assertIsNone(held_edit_body(plain))
        plain.metadata[HELD_EDIT_GUARD_KEY] = {"phase": "request"}
        plain.metadata[HELD_EDIT_BODY_KEY] = {"content": object()}
        self.assertIsNone(held_edit_guard(plain))
        self.assertIsNone(held_edit_body(plain))
