"""Behavioral safeguards for the historical weight-correction tool.

All account access is simulated in memory. No test uses a real API key or network.
"""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fix_hevy_weights as hevy


def workout_fixture():
    """Include fields whose loss would silently alter a historical workout."""
    return {
        "id": "workout-1",
        "title": "Historical strength session",
        "description": "Original workout notes",
        "routine_id": "routine-original",
        "start_time": "2026-01-01T10:00:00Z",
        "end_time": "2026-01-01T11:00:00Z",
        "is_private": True,
        "exercises": [
            {
                "index": 0,
                "title": "Bench Press (Dumbbell)",
                "exercise_template_id": "dumbbell-bench",
                "superset_id": 0,
                "notes": "Keep these exercise notes",
                "sets": [
                    {"index": 0, "type": "warmup", "weight_kg": 12.5,
                     "reps": 8, "rpe": 6, "distance_meters": None,
                     "duration_seconds": None, "custom_metric": 7},
                    {"index": 1, "type": "normal", "weight_kg": 20,
                     "reps": 10, "rpe": 8.5, "distance_meters": None,
                     "duration_seconds": None, "custom_metric": None},
                    {"index": 2, "type": "normal", "weight_kg": None,
                     "reps": 10, "rpe": None, "distance_meters": None,
                     "duration_seconds": None, "custom_metric": None},
                ],
            },
            {
                "index": 1,
                "title": "Squat (Barbell)",
                "exercise_template_id": "barbell-squat",
                "superset_id": 0,
                "notes": "Original squat cue",
                "sets": [
                    {"index": 0, "type": "normal", "weight_kg": 40,
                     "reps": 5, "rpe": 8, "distance_meters": None,
                     "duration_seconds": None, "custom_metric": None},
                    {"index": 1, "type": "warmup", "weight_kg": 0,
                     "reps": 10, "rpe": None, "distance_meters": None,
                     "duration_seconds": None, "custom_metric": None},
                ],
            },
            {
                "index": 2,
                "title": "Lat Pulldown (Cable)",
                "exercise_template_id": "cable-pulldown",
                "superset_id": None,
                "notes": "Cable stays unchanged",
                "sets": [
                    {"index": 0, "type": "normal", "weight_kg": 50,
                     "reps": 12, "rpe": 7, "distance_meters": None,
                     "duration_seconds": None, "custom_metric": 3},
                ],
            },
        ],
    }


def template_fixtures():
    return [
        {"id": "dumbbell-bench", "title": "Bench Press (Dumbbell)", "equipment": "dumbbell"},
        {"id": "barbell-squat", "title": "Squat (Barbell)", "equipment": "barbell"},
        {"id": "cable-pulldown", "title": "Lat Pulldown (Cable)", "equipment": "cable"},
    ]

class FakeAPI:
    """Simulate Hevy PUT while retaining the API's read-only identifiers."""

    def __init__(self, workouts, account_id="account-1", *, omit_visibility=False):
        self.workouts = {w["id"]: copy.deepcopy(w) for w in workouts}
        self.identity = account_id
        self.writes = []
        self.reads = []
        self.before_get = None
        self.after_update = None
        self.fail_before_write = False
        self.fail_after_write = False
        self.omit_visibility = omit_visibility

    def account_id(self):
        return self.identity

    def get_workout(self, workout_id):
        self.reads.append(workout_id)
        if self.before_get:
            self.before_get(self, workout_id)
        result = copy.deepcopy(self.workouts[workout_id])
        if self.omit_visibility:
            result.pop("is_private", None)
        return result

    def update_workout(self, workout_id, payload):
        self.writes.append((workout_id, copy.deepcopy(payload)))
        if self.fail_before_write:
            self.fail_before_write = False
            raise hevy.HevyError("Simulated connection failure before remote write")
        current = self.workouts[workout_id]
        body = payload["workout"]
        updated = copy.deepcopy(current)
        updated.update({k: copy.deepcopy(v) for k, v in body.items() if k != "exercises"})
        updated["exercises"] = []
        for old_exercise, new_exercise in zip(current["exercises"], body["exercises"]):
            exercise = {k: copy.deepcopy(v) for k, v in old_exercise.items() if k in {"index", "title"}}
            exercise.update(copy.deepcopy(new_exercise))
            exercise["sets"] = [dict(copy.deepcopy(s), index=i) for i, s in enumerate(new_exercise["sets"])]
            updated["exercises"].append(exercise)
        updated["updated_at"] = "2026-10-05T12:00:00Z"
        self.workouts[workout_id] = updated
        if self.after_update:
            self.after_update(self, workout_id)
        if self.fail_after_write:
            self.fail_after_write = False
            raise hevy.HevyError("Simulated timeout after remote write")
        return copy.deepcopy(updated)


class WeightMigrationTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "default_barbell_weight_kg": 20,
            "bar_weight_overrides": {},
            "default_is_private": None,
            "workout_privacy": {},
        }
        self.backup = {
            "account_id": "account-1",
            "workouts": [workout_fixture()],
            "exercise_templates": template_fixtures(),
        }
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(hevy.time, "sleep").start()
        mock.patch("builtins.print").start()

    def plan_and_api(self):
        return hevy.build_plan(self.backup, self.config), FakeAPI(self.backup["workouts"])

    def apply(self, plan, api, **kwargs):
        return hevy.apply_plan(plan, self.backup, api, self.run_dir, **kwargs)

    def configure_renamed_barbell_template(self):
        historical = "Behind the Back Bicep Wrist Curl (Barbell)"
        canonical = "Behind the Back Wrist Curl (Barbell)"
        exercise = self.backup["workouts"][0]["exercises"][1]
        exercise.update(exercise_template_id="barbell-wrist-curl", title=historical)
        self.backup["exercise_templates"][1].update(id="barbell-wrist-curl", title=canonical)
        return historical, canonical

    def test_exact_weight_formulas_include_empty_standard_bar(self):
        for old, equipment, bar, expected in [
            (12.5, "dumbbell", 20, 25),
            (0.1, "dumbbell", 20, 0.2),
            (0, "dumbbell", 20, 0),
            (40, "barbell", 20, 100),
            (0, "barbell", 20, 20),
            (12.5, "barbell", 15, 40),
        ]:
            with self.subTest(old=old, equipment=equipment, bar=bar):
                self.assertEqual(hevy.corrected_weight(old, equipment, bar), expected)

    def test_invalid_recorded_weights_and_bar_weights_are_rejected(self):
        for value in (-1, True, False, "20", None, float("nan"), float("inf"), float("-inf")):
            with self.subTest(weight=value):
                with self.assertRaises(hevy.HevyError):
                    hevy.corrected_weight(value, "dumbbell")
        for value in (-1, 0, True, "20", None, float("nan"), float("inf")):
            with self.subTest(bar=value):
                with self.assertRaises(hevy.HevyError):
                    hevy.corrected_weight(10, "barbell", value)
        with self.assertRaises(hevy.HevyError):
            hevy.corrected_weight(1e308, "dumbbell")

    def test_specialty_titles_excluded_even_when_metadata_says_barbell(self):
        titles = [
            "EZ Bar Curl", "E-Z Bar Curl", "Smith Machine Squat", "Trap Bar Deadlift",
            "Hex Bar Deadlift", "Safety Bar Squat", "Swiss Bar Bench", "Landmine Press",
            "Fixed Barbell Curl", "T-Bar Row",
        ]
        for title in titles:
            for location in ("exercise", "template"):
                with self.subTest(title=title, location=location):
                    exercise = {"exercise_template_id": "x", "title": title if location == "exercise" else "Press"}
                    template = {"id": "x", "title": title if location == "template" else "Press", "equipment": "barbell"}
                    self.assertIsNone(hevy.classify_exercise(exercise, {"x": template}))

    def test_metadata_controls_classification_and_missing_metadata_blocks(self):
        exercise = {"exercise_template_id": "x", "title": "Dumbbell Barbell Machine"}
        for equipment in ("machine", "cable", "bodyweight", "kettlebell", None):
            with self.subTest(equipment=equipment):
                self.assertIsNone(hevy.classify_exercise(exercise, {"x": {"equipment": equipment}}))
        self.backup["exercise_templates"] = []
        plan = hevy.build_plan(self.backup, self.config)
        self.assertTrue(plan["blockers"])
        self.assertEqual(plan["corrections"], [])
        with self.assertRaises(hevy.HevyError):
            hevy.validate_plan(plan, self.backup)

    def test_plan_contains_only_target_weights_and_does_not_mutate_backup(self):
        original = copy.deepcopy(self.backup)
        plan = hevy.build_plan(self.backup, self.config)
        values = [(c["exercise_index"], c["set_index"], c["old_weight_kg"], c["new_weight_kg"]) for c in plan["corrections"]]
        self.assertEqual(values, [(0, 0, 12.5, 25), (0, 1, 20, 40), (1, 0, 40, 100), (1, 1, 0, 20)])
        self.assertEqual(self.backup, original)
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(plan["excluded"], [{"exercise": "Lat Pulldown (Cable)", "reason": "other equipment", "sets": 1}])

    def test_bar_weight_override_uses_template_id(self):
        self.config["bar_weight_overrides"] = {"barbell-squat": 15}
        plan = hevy.build_plan(self.backup, self.config)
        barbell_changes = [c for c in plan["corrections"] if c["equipment"] == "barbell"]
        self.assertEqual([c["new_weight_kg"] for c in barbell_changes], [95, 15])
        self.assertTrue(all(c["bar_weight_kg"] == 15 for c in barbell_changes))

    def test_unknown_visibility_blocks_payload_and_apply_without_writes(self):
        del self.backup["workouts"][0]["is_private"]
        with self.assertRaisesRegex(hevy.HevyError, "visibility is unknown"):
            hevy.workout_to_update_payload(self.backup["workouts"][0], self.config)
        plan, api = self.plan_and_api()
        with self.assertRaisesRegex(hevy.HevyError, "unresolved blockers"):
            self.apply(plan, api)
        self.assertEqual(api.writes, [])

    def test_explicit_false_visibility_and_per_workout_override_are_preserved(self):
        workout = self.backup["workouts"][0]
        workout["is_private"] = False
        self.config["default_is_private"] = True
        self.assertIs(hevy.workout_to_update_payload(workout, self.config)["workout"]["is_private"], False)
        del workout["is_private"]
        self.config["workout_privacy"][workout["id"]] = False
        self.assertIs(hevy.workout_to_update_payload(workout, self.config)["workout"]["is_private"], False)

    def test_payload_preserves_documented_metrics_and_zero_superset(self):
        workout = self.backup["workouts"][0]
        body = hevy.workout_to_update_payload(workout, self.config)["workout"]
        self.assertNotIn("routine_id", body)  # Read-only in PUT; verified after update.
        self.assertEqual(body["exercises"][0]["superset_id"], 0)
        for expected, actual in zip(workout["exercises"], body["exercises"]):
            self.assertEqual(actual, {key: value for key, value in expected.items() if key not in {"title", "index", "sets"}} | {
                "sets": [{key: value for key, value in s.items() if key != "index"} for s in expected["sets"]]
            })
        self.assertEqual(body["description"], workout["description"])
        self.assertEqual(body["start_time"], workout["start_time"])
        self.assertEqual(body["end_time"], workout["end_time"])

    def test_unknown_fields_block_before_any_write_instead_of_disappearing(self):
        for level in ("workout", "exercise", "set"):
            with self.subTest(level=level):
                backup = copy.deepcopy(self.backup)
                workout = backup["workouts"][0]
                obj = workout if level == "workout" else workout["exercises"][0]
                if level == "set":
                    obj = obj["sets"][0]
                obj["future_api_field"] = "must not disappear"
                plan = hevy.build_plan(backup, self.config)
                self.assertTrue(plan["blockers"])
                with self.assertRaises(hevy.HevyError):
                    hevy.validate_plan(plan, backup)

    def test_successful_apply_changes_only_four_weights_and_updated_timestamp(self):
        plan, api = self.plan_and_api()
        result = self.apply(plan, api)
        expected = workout_fixture()
        expected["exercises"][0]["sets"][0]["weight_kg"] = 25
        expected["exercises"][0]["sets"][1]["weight_kg"] = 40
        expected["exercises"][1]["sets"][0]["weight_kg"] = 100
        expected["exercises"][1]["sets"][1]["weight_kg"] = 20
        actual = copy.deepcopy(api.workouts["workout-1"])
        actual.pop("updated_at")
        self.assertEqual(actual, expected)
        self.assertEqual(result, {"updated_workouts": 1, "already_corrected_workouts": 0, "planned_workouts": 1})
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(hevy.read_json(self.run_dir / "journal.json")["workouts"]["workout-1"]["status"], "verified")

    def test_reapplying_same_plan_is_idempotent(self):
        plan, api = self.plan_and_api()
        self.apply(plan, api)
        after_first_apply = copy.deepcopy(api.workouts)
        result = self.apply(plan, api)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.workouts, after_first_apply)
        self.assertEqual(result["updated_workouts"], 0)
        self.assertEqual(result["already_corrected_workouts"], 1)

    def test_full_preflight_detects_later_workout_drift_before_first_write(self):
        second = workout_fixture()
        second["id"] = "workout-2"
        self.backup["workouts"].append(second)
        plan, api = self.plan_and_api()
        api.workouts["workout-2"]["description"] = "User edited these notes"
        with self.assertRaisesRegex(hevy.HevyError, "changed since backup"):
            self.apply(plan, api)
        self.assertEqual(api.writes, [])

    def test_immediate_recheck_detects_edits_after_preflight(self):
        plan, api = self.plan_and_api()
        def drift_on_second_read(fake, wid):
            if len(fake.reads) == 2:
                fake.workouts[wid]["exercises"][2]["sets"][0]["reps"] = 99
        api.before_get = drift_on_second_read
        with self.assertRaisesRegex(hevy.HevyError, "changed since backup"):
            self.apply(plan, api)
        self.assertEqual(api.writes, [])

    def test_ambiguous_completed_write_recovers_from_pending_journal_without_doubling(self):
        plan, api = self.plan_and_api()
        api.fail_after_write = True
        with self.assertRaisesRegex(hevy.HevyError, "timeout after remote write"):
            self.apply(plan, api)
        journal = hevy.read_json(self.run_dir / "journal.json")
        self.assertEqual(journal["workouts"]["workout-1"]["status"], "pending")
        result = self.apply(plan, api)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.workouts["workout-1"]["exercises"][0]["sets"][0]["weight_kg"], 25)
        self.assertEqual(result["already_corrected_workouts"], 1)
        self.assertEqual(hevy.read_json(self.run_dir / "journal.json")["workouts"]["workout-1"]["status"], "verified")

    def test_ambiguous_uncompleted_write_can_be_retried_from_original_state(self):
        plan, api = self.plan_and_api()
        api.fail_before_write = True
        with self.assertRaises(hevy.HevyError):
            self.apply(plan, api)
        self.assertEqual(api.workouts["workout-1"], self.backup["workouts"][0])
        result = self.apply(plan, api)
        self.assertEqual(len(api.writes), 2)
        self.assertEqual(result["updated_workouts"], 1)
        self.assertEqual(api.workouts["workout-1"]["exercises"][0]["sets"][0]["weight_kg"], 25)

    def test_post_write_routine_link_loss_stops_with_verification_failure(self):
        second = workout_fixture()
        second["id"] = "workout-2"
        self.backup["workouts"].append(second)
        plan, api = self.plan_and_api()
        api.after_update = lambda fake, wid: fake.workouts[wid].update(routine_id=None)
        with self.assertRaisesRegex(hevy.HevyError, "Verification failed"):
            self.apply(plan, api)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.workouts["workout-2"], second)
        self.assertEqual(hevy.read_json(self.run_dir / "journal.json")["workouts"]["workout-1"]["status"], "verification_failed")

    def test_tampered_plan_or_backup_is_rejected_without_writes(self):
        for tamper in ("weight", "reps", "backup"):
            with self.subTest(tamper=tamper):
                plan, api = self.plan_and_api()
                backup = copy.deepcopy(self.backup)
                if tamper == "backup":
                    backup["workouts"][0]["description"] = "Unexpected backup change"
                else:
                    plan["corrections"][0]["new_weight_kg" if tamper == "weight" else "reps"] = 999
                with self.assertRaisesRegex(hevy.HevyError, "Plan differs"):
                    hevy.apply_plan(plan, backup, api, self.run_dir)
                self.assertEqual(api.writes, [])

    def test_journal_for_another_plan_is_rejected_without_writes(self):
        plan, api = self.plan_and_api()
        hevy.save_json(self.run_dir / "journal.json", {"plan_sha256": "another-plan", "workouts": {}})
        with self.assertRaisesRegex(hevy.HevyError, "Journal belongs to a different plan"):
            self.apply(plan, api)
        self.assertEqual(api.writes, [])

    def test_wrong_account_is_rejected_without_writes(self):
        plan, api = self.plan_and_api()
        api.identity = "somebody-else"
        with self.assertRaisesRegex(hevy.HevyError, "different account"):
            self.apply(plan, api)
        self.assertEqual(api.writes, [])

    def test_limit_then_resume_updates_each_workout_once(self):
        second = workout_fixture()
        second["id"] = "workout-2"
        self.backup["workouts"].append(second)
        plan, api = self.plan_and_api()
        first = self.apply(plan, api, limit=1)
        self.assertEqual(first["updated_workouts"], 1)
        self.assertEqual(api.workouts["workout-2"], second)
        second_result = self.apply(plan, api)
        self.assertEqual(second_result["updated_workouts"], 1)
        self.assertEqual(second_result["already_corrected_workouts"], 1)
        self.assertEqual([wid for wid, _ in api.writes], ["workout-1", "workout-2"])

    def test_final_verification_checks_untargeted_workouts_too(self):
        untouched = workout_fixture()
        untouched["id"] = "workout-cable-only"
        untouched["exercises"] = [untouched["exercises"][2]]
        untouched["exercises"][0]["index"] = 0
        self.backup["workouts"].append(untouched)
        plan, api = self.plan_and_api()
        self.apply(plan, api)
        self.assertEqual([wid for wid, _ in api.writes], ["workout-1"])
        report = hevy.verify_plan(plan, self.backup, api)
        self.assertEqual(report["workouts_checked"], 2)
        self.assertEqual(report["corrected_sets"], 4)
        api.workouts["workout-cable-only"]["title"] = "Unexpected edit"
        with self.assertRaisesRegex(hevy.HevyError, "Verification mismatch"):
            hevy.verify_plan(plan, self.backup, api)

    def test_final_verification_requires_visibility_only_for_targeted_workouts(self):
        del self.backup["workouts"][0]["is_private"]
        untouched = workout_fixture()
        untouched["id"] = "workout-cable-only"
        del untouched["is_private"]
        untouched["exercises"] = [untouched["exercises"][2]]
        untouched["exercises"][0]["index"] = 0
        self.backup["workouts"].append(untouched)
        self.config["workout_privacy"] = {"workout-1": False}
        self.assertIsNone(self.config["default_is_private"])
        plan = hevy.build_plan(self.backup, self.config)
        self.assertEqual(plan["blockers"], [])
        api = FakeAPI(self.backup["workouts"], omit_visibility=True)

        self.apply(plan, api)
        self.assertEqual([wid for wid, _ in api.writes], ["workout-1"])
        self.assertIs(api.writes[0][1]["workout"]["is_private"], False)
        self.assertNotIn("is_private", api.get_workout("workout-1"))
        self.assertNotIn("is_private", api.get_workout("workout-cable-only"))
        self.assertEqual(api.workouts["workout-cable-only"], untouched)
        report = hevy.verify_plan(plan, self.backup, api)
        self.assertEqual(report["workouts_checked"], 2)
        self.assertEqual(report["corrected_sets"], 4)

        # Missing visibility must not weaken checks of observable, untouched data.
        api.workouts["workout-cable-only"]["exercises"][0]["sets"][0]["weight_kg"] = 999
        with self.assertRaisesRegex(hevy.HevyError, "Verification mismatch"):
            hevy.verify_plan(plan, self.backup, api)

    def test_ambiguous_write_recovery_when_get_omits_targeted_workout_visibility(self):
        del self.backup["workouts"][0]["is_private"]
        self.config["workout_privacy"] = {"workout-1": True}
        plan = hevy.build_plan(self.backup, self.config)
        api = FakeAPI(self.backup["workouts"], omit_visibility=True)
        api.fail_after_write = True
        with self.assertRaisesRegex(hevy.HevyError, "timeout after remote write"):
            self.apply(plan, api)
        self.assertNotIn("is_private", api.get_workout("workout-1"))

        result = self.apply(plan, api)
        self.assertEqual(len(api.writes), 1)
        self.assertIs(api.writes[0][1]["workout"]["is_private"], True)
        self.assertEqual(result["updated_workouts"], 0)
        self.assertEqual(result["already_corrected_workouts"], 1)
        report = hevy.verify_plan(plan, self.backup, api)
        self.assertEqual(report["workouts_checked"], 1)
        self.assertIn("GET does not expose", report["visibility_note"])

    def test_service_canonical_exercise_title_normalization_passes_post_write_verification(self):
        historical, canonical = self.configure_renamed_barbell_template()
        plan, api = self.plan_and_api()
        plan_hash = hevy.digest(plan)
        backup_hash = hevy.digest(self.backup)
        def normalize_title(fake, wid):
            fake.workouts[wid]["exercises"][1]["title"] = canonical
        api.after_update = normalize_title

        result = self.apply(plan, api)
        self.assertEqual(result["updated_workouts"], 1)
        self.assertEqual(api.workouts["workout-1"]["exercises"][1]["title"], canonical)
        self.assertEqual(api.workouts["workout-1"]["exercises"][1]["exercise_template_id"], "barbell-wrist-curl")
        self.assertEqual(self.backup["workouts"][0]["exercises"][1]["title"], historical)
        self.assertEqual(hevy.digest(plan), plan_hash)
        self.assertEqual(hevy.digest(self.backup), backup_hash)
        self.assertEqual(hevy.verify_plan(plan, self.backup, api)["corrected_sets"], 4)
        self.assertEqual(self.apply(plan, api)["already_corrected_workouts"], 1)
        self.assertEqual(len(api.writes), 1)

    def test_existing_failed_verification_journal_recovers_canonical_title_without_rewrite(self):
        _, canonical = self.configure_renamed_barbell_template()
        plan, api = self.plan_and_api()
        plan_hash = hevy.digest(plan)
        backup_hash = hevy.digest(self.backup)
        corrected = copy.deepcopy(self.backup["workouts"][0])
        for exercise_index, set_index, value in [(0, 0, 25), (0, 1, 40), (1, 0, 100), (1, 1, 20)]:
            corrected["exercises"][exercise_index]["sets"][set_index]["weight_kg"] = value
        corrected["exercises"][1]["title"] = canonical
        api.workouts["workout-1"] = corrected
        hevy.save_json(self.run_dir / "journal.json", {
            "plan_sha256": plan_hash,
            "workouts": {"workout-1": {"status": "verification_failed", "at": "2026-10-05T12:00:00Z"}},
        })

        result = self.apply(plan, api)
        self.assertEqual(result["updated_workouts"], 0)
        self.assertEqual(result["already_corrected_workouts"], 1)
        self.assertEqual(api.writes, [])
        self.assertEqual(api.workouts["workout-1"], corrected)
        journal = hevy.read_json(self.run_dir / "journal.json")
        self.assertEqual(journal["workouts"]["workout-1"]["status"], "verified")
        self.assertEqual(journal["plan_sha256"], plan_hash)
        self.assertEqual(hevy.digest(plan), plan_hash)
        self.assertEqual(hevy.digest(self.backup), backup_hash)
        self.assertEqual(hevy.verify_plan(plan, self.backup, api)["corrected_sets"], 4)

    def test_arbitrary_exercise_title_change_stops_and_cannot_resume(self):
        self.configure_renamed_barbell_template()
        plan, api = self.plan_and_api()
        def change_title(fake, wid):
            fake.workouts[wid]["exercises"][1]["title"] = "Unrecognized Replacement Exercise"
        api.after_update = change_title
        with self.assertRaisesRegex(hevy.HevyError, "Verification failed"):
            self.apply(plan, api)
        with self.assertRaisesRegex(hevy.HevyError, "changed since backup"):
            self.apply(plan, api)
        self.assertEqual(len(api.writes), 1)

    def test_canonical_title_cannot_hide_changed_exercise_template_id(self):
        _, canonical = self.configure_renamed_barbell_template()
        plan, api = self.plan_and_api()
        def replace_exercise(fake, wid):
            fake.workouts[wid]["exercises"][1].update(title=canonical, exercise_template_id="different-template")
        api.after_update = replace_exercise
        with self.assertRaisesRegex(hevy.HevyError, "Verification failed"):
            self.apply(plan, api)
        with self.assertRaisesRegex(hevy.HevyError, "changed since backup"):
            self.apply(plan, api)
        self.assertEqual(len(api.writes), 1)

    def test_canonical_exercise_title_cannot_hide_changed_workout_title(self):
        _, canonical = self.configure_renamed_barbell_template()
        plan, api = self.plan_and_api()
        def change_titles(fake, wid):
            fake.workouts[wid]["exercises"][1]["title"] = canonical
            fake.workouts[wid]["title"] = "Different workout title"
        api.after_update = change_titles
        with self.assertRaisesRegex(hevy.HevyError, "Verification failed"):
            self.apply(plan, api)
        self.assertEqual(len(api.writes), 1)

    def test_final_verification_rejects_canonical_title_drift_in_untouched_workout(self):
        untouched = workout_fixture()
        untouched["id"] = "workout-cable-only"
        untouched["exercises"] = [untouched["exercises"][2]]
        untouched["exercises"][0]["index"] = 0
        untouched["exercises"][0]["title"] = "Historical Cable Exercise Title"
        self.backup["workouts"].append(untouched)
        plan, api = self.plan_and_api()
        self.apply(plan, api)
        self.assertEqual([wid for wid, _ in api.writes], ["workout-1"])
        # Canonical names are tolerated only for workouts the migration updates.
        api.workouts["workout-cable-only"]["exercises"][0]["title"] = "Lat Pulldown (Cable)"
        with self.assertRaisesRegex(hevy.HevyError, "Verification mismatch"):
            hevy.verify_plan(plan, self.backup, api)

    def test_canonical_machine_title_change_in_affected_workout_still_fails(self):
        self.backup["workouts"][0]["exercises"][2]["title"] = "Historical Machine Exercise Name"
        self.backup["exercise_templates"][2].update(title="Lat Pulldown (Machine)", equipment="machine")
        plan, api = self.plan_and_api()
        def normalize_machine_title(fake, wid):
            fake.workouts[wid]["exercises"][2]["title"] = "Lat Pulldown (Machine)"
        api.after_update = normalize_machine_title
        with self.assertRaisesRegex(hevy.HevyError, "Verification failed"):
            self.apply(plan, api)
        self.assertEqual(len(api.writes), 1)
        with self.assertRaisesRegex(hevy.HevyError, "changed since backup"):
            self.apply(plan, api)
        self.assertEqual(len(api.writes), 1)


if __name__ == "__main__":
    unittest.main()
