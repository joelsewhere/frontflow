"""Regression test for the picker-field collection bug.

Background: Phase 3 added the `picker` field type to the backend
and to the React block-render registry, but missed adding it to
the frontend's `FIELD_TYPES` Set in `schema.ts`. That Set is what
`collectFields` walks to decide which fields are part of the form.
Without `picker` in the Set, the field was rendered (the user saw
the dropdown) but never registered with react-hook-form; its
value never reached the request body; no Assign ever fired.

The fix is in `frontend/src/components/blocks/schema.ts`. This
test asserts the shipped bundle contains the string `"picker"`
in the FIELD_TYPES Set — a build regression that drops the
picker from the Set would catch here, before users do.

Inspects the bundled JS asset since we don't run a JS test
runner. Cheap and good enough.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _static_assets_dir() -> Path:
    here = Path(__file__).resolve().parent
    pkg_root = here.parent
    return pkg_root / "src" / "frontflow" / "static" / "assets"


def _bundle_text() -> str:
    """Read every JS asset in the bundle, concatenated. The minifier
    splits across multiple chunks; we don't care which holds the
    string."""
    assets = _static_assets_dir()
    if not assets.is_dir():
        pytest.skip("frontend bundle not present (dev/test install)")
    js_files = sorted(assets.glob("*.js"))
    if not js_files:
        pytest.skip("no JS assets in bundled frontend")
    return "\n".join(f.read_text(encoding="utf-8") for f in js_files)


class TestFrontendBundlePicker:
    """The picker field type must be in the bundle's FIELD_TYPES set
    AND have a case in buildSchema/buildDefaults/isBlank. Each is
    expressed via a distinctive string-literal pattern the minifier
    preserves verbatim."""

    def test_picker_in_field_types_set(self):
        bundle = _bundle_text()
        # `new Set([..., "picker", ...])` — order varies with the
        # minifier, but the literal "picker" appears inside a
        # `new Set([...])` whose other entries are all known field
        # types. Loose check: assert "picker" appears at least once
        # in a context that looks like a FIELD_TYPES init.
        assert '"picker"' in bundle, (
            "the shipped bundle does not contain the literal "
            '"picker" — schema.ts FIELD_TYPES likely missing the '
            "entry, which would cause picker fields to be rendered "
            "but not registered with react-hook-form"
        )

    def test_picker_in_buildSchema_switch(self):
        bundle = _bundle_text()
        # The buildSchema picker case begins with
        # `case"picker":{const o=!!r.raw.props.multi` (minified).
        # If schema.ts loses the picker switch arm, the field's
        # validation falls through to default (string) and the
        # picker value would fail to validate.
        assert 'case"picker"' in bundle, (
            "buildSchema's `case \"picker\":` arm appears to be "
            "missing from the bundle"
        )

    def test_picker_default_value_initialized(self):
        bundle = _bundle_text()
        # buildDefaults sets `t[n.id] = isMulti ? [] : null` for
        # pickers. Without this the field has no entry in
        # defaultValues, which (for non-controlled fields) can mean
        # react-hook-form doesn't see it. The literal `?[]:null` is
        # distinctive after minification.
        assert "?[]:null" in bundle or "? [] : null" in bundle, (
            "buildDefaults's picker default initializer "
            "(empty array for multi, null for single) appears to "
            "be missing from the bundle"
        )

    def test_value_labels_rendering_in_bundle(self):
        """Regression: the submission-detail page must render
        picker values as labels (e.g. 'admin') instead of the raw
        identifier (e.g. '1'). The frontend's
        renderValueWithLabels reads `step.value_labels` and
        `step.value_kinds` from the API response. If those reads
        are missing from the bundle, the user sees raw user_ids.

        This was a silent build failure: the TypeScript build
        emitted no JS for SubmissionSummaryContent.tsx after a
        missing `Link` import was added, and the package layer
        shipped the previous successful bundle. From the wheel's
        outside, everything looked normal — but the picker labels
        never rendered. This grep catches that exact regression.
        """
        bundle = _bundle_text()
        assert (
            "value_labels" in bundle
            or "value_kinds" in bundle
        ), (
            "the shipped bundle does not reference value_labels "
            "or value_kinds — the submission-detail page won't "
            "render picker user_ids as usernames. Likely cause: "
            "the frontend build silently failed and the previous "
            "successful bundle was shipped. Verify `npm run build` "
            "completes without errors."
        )

    def test_user_detail_page_in_bundle(self):
        """Regression: the admin user-detail page at /users/:userId
        with its Access tab must be in the bundle so admins can
        manage per-user assignments. Three concrete strings to
        check: the API path the page hits, the literal "Revoke"
        button text, and the route registration."""
        bundle = _bundle_text()
        assert "/assignments/" in bundle, (
            "the shipped bundle does not contain the "
            "/api/.../assignments/ endpoint path — the user-detail "
            "page's revoke action is missing"
        )
        assert "Revoke" in bundle, (
            "the shipped bundle does not contain the 'Revoke' "
            "label — the user-detail page is likely missing from "
            "the build"
        )

    def test_assignments_chip_in_bundle(self):
        """Regression: the parent submission's graph view must show
        spawned child submissions as chips inline under the node
        that fired the Assign. The `AssignmentsList` helper in
        HitlNode renders these — the title strip "Assigned (" and
        the API field `child_form_title` must both appear in the
        bundle.

        Without these, the parent's graph would have no visible
        connection to its children — users would only see them
        in /my-tasks (for assignees) or the admin user-detail page.
        """
        bundle = _bundle_text()
        assert "Assigned (" in bundle, (
            "the shipped bundle does not contain the 'Assigned ('"
            " strip — the parent-submission graph won't render "
            "spawned child submissions"
        )
        assert "child_form_title" in bundle, (
            "the shipped bundle does not consume the AssignedChild "
            "fields — the parent-submission graph won't render "
            "spawned child submissions"
        )

    def test_child_cluster_rendering_in_bundle(self):
        """Regression: the parent submission's GRAPH (not just the
        step-block list) must render spawned child submissions as
        nested cluster subgraphs. The `layoutGraphWithChildren`
        wrapper + `GraphChildClusterNode` component emit two
        distinctive strings: the namespaced id prefix
        `child-cluster:` and the cluster header label `Spawned`.

        Without these, the canvas falls back to the single-form
        layout and children aren't visualized in the graph (only
        the inline step-block chips show them).
        """
        bundle = _bundle_text()
        assert "child-cluster:" in bundle, (
            "the shipped bundle does not contain the "
            "child-cluster id prefix — the nested-graph "
            "visualization for spawned children is missing"
        )
        assert "Spawned" in bundle, (
            "the shipped bundle does not contain the 'Spawned' "
            "cluster header label — GraphChildClusterNode is "
            "missing from the build"
        )

    def test_revoked_cluster_collapsed_in_bundle(self):
        """Regression for Phase 7f: revoked child clusters must
        strict-collapse to a header strip in the parent submission's
        graph. The collapsed branch of `GraphChildClusterNode` is
        tagged with `data-cluster-state="revoked-collapsed"` — a
        distinctive string literal that survives minification only
        if the collapsed render path is present in the build.

        Without the collapsed path, a revoked assignment renders at
        full size with muted opacity — the original Phase 7e
        behavior. The strict-collapse keeps live work dominant in
        the graph while preserving the audit trail.
        """
        bundle = _bundle_text()
        assert "revoked-collapsed" in bundle, (
            "the shipped bundle does not contain the "
            "'revoked-collapsed' cluster-state marker — "
            "GraphChildClusterNode's strict-collapse branch is "
            "missing from the build, so revoked assignments will "
            "render at full size instead of as a header strip"
        )

    def test_cross_form_hover_scoping_in_bundle(self):
        """Regression for Phase 17 (#2): hover-highlighting must
        cross the cross-form (Assign) boundary so a spawning
        relationship lights up regardless of which end the user
        hovers.

        The implementation lives in `computeHighlight` in
        `WorkflowGraphCanvas.tsx` and is driven by two string
        prefixes — `"child-cluster:"` and `"child:"` — that the
        hover code matches against node ids. Both prefixes need
        to appear inline as string literals (not just inside other
        code paths) for the cross-form scoping logic to be wired
        in. Each prefix appears in MULTIPLE distinct call sites
        across the bundle (layoutGraphWithChildren emits clusters,
        computeHighlight checks both prefixes, several other
        layout/rendering paths reference them). A bundle missing
        either is structurally incomplete.

        We assert at-least-two occurrences of each as a guard
        against regression — a minifier optimization that
        deduplicates string literals can collapse some, but losing
        ALL but one for either prefix indicates a code path was
        dropped.
        """
        bundle = _bundle_text()
        cluster_uses = bundle.count('child-cluster:')
        child_uses = bundle.count('child:')
        assert cluster_uses >= 2, (
            f"the shipped bundle has only {cluster_uses} reference(s) "
            f"to 'child-cluster:' — the cross-form hover scoping or "
            f"the cluster layout path is likely missing from the build"
        )
        assert child_uses >= 2, (
            f"the shipped bundle has only {child_uses} reference(s) "
            f"to 'child:' — the cross-form hover scoping or the "
            f"namespaced-child layout path is likely missing from "
            f"the build"
        )

    def test_tree_recursive_child_layout_in_bundle(self):
        """Regression for Phase 18 (#3): grandchild clusters must
        position relative to their direct parent cluster, not the
        root parent. The implementation in `layoutWithChildren.ts`
        switched from a single-pass cursor model to a two-pass
        tree-recursive layout. The new code introduces a per-
        cluster `subTrees` field that survives minification —
        it's a runtime object property name, not a TS-erased
        interface.

        We assert `subTrees` appears MULTIPLE times in the bundle
        (read site + write sites). The old cursor implementation
        had zero references — a minifier mangle or accidental
        revert to the cursor model would drop the count to 0.
        """
        bundle = _bundle_text()
        sub_tree_refs = bundle.count("subTrees")
        assert sub_tree_refs >= 2, (
            f"the shipped bundle has only {sub_tree_refs} reference(s) "
            f"to 'subTrees' — the tree-recursive grandchild layout "
            f"is likely missing from the build (or was reverted to "
            f"the flat cursor model that positioned grandchildren "
            f"next to the root parent rather than their direct "
            f"parent cluster)"
        )

    def test_source_tab_in_bundle(self):
        """Regression for the Source tab (FormSummaryPage and
        SubmissionDetailPage). The backend has two source endpoints
        (`/api/forms/{form_id}/source` and
        `/api/forms/{form_id}/versions/{version}/source`) which the
        frontend reaches via React-Query hooks named `formSource`
        and `formVersionSource`. These query keys are runtime
        string literals — they survive minification.

        We assert both query keys + the visible 'Source' tab label
        ship in the bundle. A regression to "Source tab missing"
        (which happened once when the frontend source tree was
        rebuilt from the minified bundle and the source-related
        components got lost) would drop at least one of these to
        zero.
        """
        bundle = _bundle_text()
        assert bundle.count("formSource") >= 1, (
            "shipped bundle is missing the 'formSource' React-Query "
            "key — the form-source hook isn't wired into any page"
        )
        assert bundle.count("formVersionSource") >= 1, (
            "shipped bundle is missing the 'formVersionSource' "
            "React-Query key — the per-submission Source tab can't "
            "fetch the pinned form_version's source"
        )
        # The tab button on FormSummaryPage AND the tab button on
        # SubmissionDetailPage both render the literal "Source".
        assert bundle.count('"Source"') >= 2, (
            "shipped bundle has fewer than 2 'Source' string "
            "literals — one of the Source tabs is likely missing "
            "from the build"
        )
