"""Deterministic lesson safety tests, not claims of live semantic accuracy.

The hand-labelled fixtures exercise editing invariants across subjects/styles.
Cloud models are mocked; teacher-reviewed real videos are a separate evaluation.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import lecture_trimmer.analysis.classifier as classifier
import lecture_trimmer.config as config
import lecture_trimmer.analysis.lesson_classifier as lessons
from lecture_trimmer.processing.lesson_planner import plan_activities
from lecture_trimmer.models import (ActivityEdit, ActivityKind, ActivityRole, LessonEdits, LessonMap, PassageCut,
                    Retention, TeachingActivity, Unit)


def units(count, seconds=10):
    return [Unit(id=i, start=i * seconds, end=(i + 1) * seconds,
                 text=f"Hand-labelled teaching unit {i}") for i in range(count)]


ROLE_FOR = {ActivityKind.THEORY: ActivityRole.CONCEPT, ActivityKind.QUESTIONS: ActivityRole.TASK,
            ActivityKind.NONACADEMIC: ActivityRole.OFF_TOPIC, ActivityKind.UNCERTAIN: ActivityRole.UNCLEAR}


def activity(first, last, kind=ActivityKind.THEORY, **kwargs):
    kwargs.setdefault("role", ROLE_FOR[kind])
    return TeachingActivity(first_id=first, last_id=last, kind=kind,
                            title="Complete teaching activity", summary="Setup, reasoning and conclusion", **kwargs)


def cut(first, last, category=Retention.NONACADEMIC):
    return PassageCut(first_id=first, last_id=last, category=category,
                      standalone=True, reason="Independent unrelated interruption")


def edit(aid, cuts=(), essential=(), **kwargs):
    return ActivityEdit(activity_id=aid, cuts=list(cuts), essential_ids=list(essential),
                        review_required=kwargs.get("review_required", False),
                        review_reason=kwargs.get("review_reason", ""),
                        subactivities=kwargs.get("subactivities", []))


def intervals(plan):
    return [(item.start, item.end) for item in plan]


class LessonValidationTests(unittest.TestCase):
    def test_map_requires_complete_nonoverlapping_coverage(self):
        source = units(6)
        for mapped in [LessonMap(activities=[activity(0, 4)]),
                       LessonMap(activities=[activity(0, 3), activity(3, 5)]),
                       LessonMap(activities=[activity(0, 6)])]:
            with self.subTest(mapped=mapped), self.assertRaises(ValueError):
                lessons.validate_map(mapped, source)
        valid = lessons.validate_map(LessonMap(activities=[activity(3, 5), activity(0, 2)]), source)
        self.assertEqual([item.first_id for item in valid], [0, 3])

    def test_dependencies_must_be_earlier_academic_activities(self):
        for mapped in [
            [activity(0, 0, depends_on=[1]), activity(1, 2)],
            [activity(0, 0, ActivityKind.NONACADEMIC), activity(1, 2, depends_on=[0])],
            [activity(0, 2, depends_on=[0])],
        ]:
            with self.subTest(mapped=mapped), self.assertRaises(ValueError):
                lessons.validate_map(LessonMap(activities=mapped), units(3))

    def test_missing_duplicate_and_essential_overlap_edits_are_rejected(self):
        mapped = [activity(0, 2), activity(3, 5)]
        for edited in [LessonEdits(activities=[edit(0)]),
                       LessonEdits(activities=[edit(0), edit(0)]),
                       LessonEdits(activities=[edit(0, [cut(1, 1)], [1]), edit(3)])]:
            with self.subTest(edited=edited), self.assertRaises(ValueError):
                lessons.validate_edits(edited, mapped, units(6))

    def test_tightening_cut_keeps_clean_cut_rules_without_failing_the_lecture(self):
        # Ten 4-second units of one explanation.
        source = [Unit(id=i, start=i*4, end=(i+1)*4, text=f"step {i}") for i in range(10)]
        mapped = [activity(0, 9)]
        accepted = lessons.validate_edits(LessonEdits(activities=[edit(0, [cut(9, 9, Retention.FILLER)])]),
                                          mapped, source)
        self.assertEqual([(c.first_id, c.category) for c in accepted[0].cuts], [(9, Retention.FILLER)])
        # A polish cut on an essential unit, one that strands a 4-second piece, and
        # tightening of more than a third of the passage are all dropped, not raised.
        # Cutting unit 1 would strand the 4-second unit 0; the cut of unit 3 is clean.
        for cuts, essential, expected in [([cut(4, 4, Retention.REPETITION)], [4], set()),
                                          ([cut(1, 1, Retention.FILLER), cut(3, 3, Retention.FILLER)], [], {3}),
                                          ([cut(5, 9, Retention.REPETITION)], [], set())]:
            with self.subTest(cuts=cuts):
                checked = lessons.validate_edits(LessonEdits(activities=[edit(0, cuts, essential)]),
                                                 mapped, source)
                self.assertEqual({c.first_id for c in checked[0].cuts}, expected)

    def test_unrelated_passage_rules_stay_strict(self):
        source = [Unit(id=i, start=i*4, end=(i+1)*4, text=f"step {i}") for i in range(10)]
        with self.assertRaises(ValueError):
            lessons.validate_edits(LessonEdits(activities=[edit(0, [cut(4, 4)], [4])]), [activity(0, 9)], source)

    def test_role_decides_the_video_and_the_model_must_supply_it(self):
        task = TeachingActivity.model_validate({"first_id": 0, "last_id": 2, "role": "task",
                                                "title": "Find speed", "summary": "Solved"})
        illustration = TeachingActivity.model_validate({"first_id": 3, "last_id": 4, "role": "illustration",
                                                        "title": "Example", "summary": "Explains"})
        self.assertEqual((task.kind, illustration.kind), (ActivityKind.QUESTIONS, ActivityKind.THEORY))
        self.assertNotIn("kind", TeachingActivity.model_json_schema()["properties"])
        with self.assertRaisesRegex(ValueError, "role"):
            lessons.require_roles([TeachingActivity(first_id=0, last_id=1, kind=ActivityKind.THEORY,
                                                    title="", summary="")])

    def test_proposed_cut_cannot_strand_tiny_explanation_fragments(self):
        source = [Unit(id=0, start=0, end=2, text="Maximum height ..."),
                  Unit(id=1, start=2, end=10, text="Standalone joke"),
                  Unit(id=2, start=10, end=12, text="... follows from this equation.")]
        with self.assertRaisesRegex(lessons.StudyReviewRequired, "short explanation fragment"):
            lessons.validate_edits(LessonEdits(activities=[edit(0, [cut(1, 1)])]),
                                   [activity(0, 2)], source)

    def test_complete_short_answer_is_never_deleted_for_its_duration(self):
        source = units(1, seconds=2)
        mapped = [activity(0, 0, ActivityKind.QUESTIONS)]
        labels = lessons.make_labels(source, mapped, [edit(0, essential=[0])])
        planned, _ = plan_activities(source, labels, 2, padding=0)
        self.assertEqual(intervals(planned["questions"]), [(0, 2)])

    def test_whole_academic_deletion_requires_explicit_category_correction(self):
        source = units(3)
        with self.assertRaisesRegex(lessons.StudyReviewRequired, "delete all mapped teaching"):
            lessons.validate_edits(LessonEdits(activities=[edit(0, [cut(0, 2)])]),
                                   [activity(0, 2)], source)
        reviewed = [edit(0, [cut(0, 2)], subactivities=[activity(0, 2, ActivityKind.NONACADEMIC)])]
        checked = lessons.validate_study_edits(LessonEdits(activities=reviewed), [activity(0, 2)], source)
        refined, edits = lessons.refine_activities(source, [activity(0, 2)], checked)
        plans, _ = plan_activities(source, lessons.make_labels(source, refined, edits), 30, padding=0)
        self.assertTrue(all(not ranges for ranges in plans.values()))

    def test_uncertain_edits_are_guarded_in_cache_validation_too(self):
        source = units(3)
        mapped = [activity(0, 2, ActivityKind.UNCERTAIN)]
        validated = lessons.validate_edits(LessonEdits(activities=[edit(0, [cut(1, 1)])]), mapped, source)
        labels = lessons.make_labels(source, mapped, validated)
        with self.assertRaises(lessons.StudyReviewRequired):
            plan_activities(source, labels, 30, padding=0)
        with self.assertRaises(lessons.StudyReviewRequired):
            lessons.validate_study_edits(LessonEdits(activities=validated), mapped, source)

    def test_unconfirmed_nonacademic_map_requires_correction_not_rescue(self):
        source = units(3)
        mapped = [activity(0, 2, ActivityKind.NONACADEMIC)]
        with self.assertRaises(lessons.StudyReviewRequired):
            lessons.make_labels(source, mapped, [edit(0, [cut(1, 1)])])
        with self.assertRaisesRegex(lessons.StudyReviewRequired, "nonacademic IDs"):
            lessons.validate_study_edits(LessonEdits(activities=[edit(0, [cut(1, 1)])]), mapped, source)

    def test_confirmed_poll_with_silence_does_not_reappear_as_study(self):
        source = units(5)
        source[0].text = "Homework kiya? Thumbs up karo."
        source[1].text = "[No reliable speech transcript; possible waiting.]"
        source[2].text = "Only 37 percent submitted; closing the poll."
        source[3].text = "Given u=40 m/s, find velocity after 3 seconds."
        source[4].text = "Gravity is negative in this direction; v=u-gt=10 m/s."
        mapped = [activity(0, 2, ActivityKind.NONACADEMIC), activity(3, 4, ActivityKind.QUESTIONS)]
        checked = lessons.validate_study_edits(LessonEdits(activities=[edit(0, [cut(0, 2)]),
            edit(3, essential=[3, 4])]), mapped, source)
        plans, context = plan_activities(source, lessons.make_labels(source, mapped, checked), 50, padding=0)
        self.assertEqual(plans["theory"], [])
        self.assertEqual(intervals(plans["questions"]), [(30, 50)])
        self.assertEqual(intervals(plans["combined"]), [(30, 50)])
        self.assertEqual(context["questions"]["uncertain_activity_ids"], [])

    def test_cut_across_protected_gap_is_trimmed_to_the_speech_beside_it(self):
        source = units(5)
        source[2].text = "[No reliable speech transcript; possible board work.]"
        # A real reply cut chatter together with the silent board-work unit. The gap is
        # kept and the chatter on both sides is still removed; the lecture does not fail.
        trimmed = lessons.validate_study_edits(LessonEdits(activities=[edit(0, [cut(1, 3)], [0, 4])]),
                                               [activity(0, 4, ActivityKind.QUESTIONS)], source)
        self.assertEqual([(c.first_id, c.last_id) for c in trimmed[0].cuts], [(1, 1), (3, 3)])
        self.assertTrue(trimmed[0].review_required)
        checked = lessons.validate_study_edits(LessonEdits(activities=[edit(0, [cut(1, 1), cut(3, 3)], [0, 4])]),
                                              [activity(0, 4, ActivityKind.QUESTIONS)], source)
        self.assertEqual([(c.first_id, c.last_id) for c in checked[0].cuts], [(1, 1), (3, 3)])

    def test_model_cannot_offer_a_cut_reason_the_validator_rejects(self):
        from lecture_trimmer.models import CutCategory
        # The rejected live reply used 'unintelligible'; it is no longer in the schema.
        self.assertNotIn("unintelligible", [c.value for c in CutCategory])
        with self.assertRaises(ValueError):
            PassageCut(first_id=0, last_id=0, reason="garbled", category="unintelligible", standalone=True)

    def test_a_batch_that_fails_twice_is_retried_in_halves(self):
        source = units(4, seconds=10)
        mapped = LessonMap(activities=[activity(0, 1), activity(2, 3)])
        seen = []
        def respond(client, backend, prompt, system, schema):
            if schema is LessonMap:
                return mapped
            data = json.loads(prompt.split("Activities to review:\n", 1)[1].split("\nFor an activity", 1)[0])
            seen.append([item["first_id"] for item in data])
            if len(data) > 1:
                return LessonEdits(activities=[])  # Invalid for the combined batch.
            return LessonEdits(activities=[edit(data[0]["first_id"])])
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as directory, \
             patch.object(config, "TEMP_DIR", Path(directory)), \
             patch("google.genai.Client", return_value=MagicMock()), \
             patch.object(classifier, "_request", side_effect=respond):
            result = lessons.classify_activities(source, backend="gemini")
            self.assertEqual(len(result.activities), 2)
            self.assertEqual(seen, [[0, 2], [0, 2], [0], [2]])
            # Both rejected replies are kept for diagnosis.
            self.assertEqual(len(list((Path(directory) / "rejected_replies").glob("*.json"))), 2)

    def test_mislabeled_wrapup_must_preserve_real_doubt_with_correct_category(self):
        source = units(5)
        source[0].text = "DPP solutions will arrive tomorrow."
        source[1].text = "Why is g negative? Upward was chosen as positive."
        source[2].text = "[No reliable speech transcript; possible equation writing.]"
        source[3].text = "Hence v=u-gt gives the answer to this exercise."
        source[4].text = "Goodnight, work hard and never give up."
        mapped = [activity(0, 4, ActivityKind.NONACADEMIC)]
        reviewed = [edit(0, [cut(0, 0), cut(4, 4)], [1, 3], subactivities=[
            activity(0, 0, ActivityKind.NONACADEMIC), activity(1, 3, ActivityKind.QUESTIONS),
            activity(4, 4, ActivityKind.NONACADEMIC)])]
        lessons.validate_study_edits(LessonEdits(activities=reviewed), mapped, source)
        refined, edits = lessons.refine_activities(source, mapped, reviewed)
        plans, _ = plan_activities(source, lessons.make_labels(source, refined, edits), 50, padding=0)
        self.assertEqual(intervals(plans["questions"]), [(10, 40)])
        self.assertEqual(plans["theory"], [])

    def test_short_theory_within_solution_stays_in_question_video(self):
        source = [Unit(id=0, start=0, end=10, text="Find speed at the highest point."),
                  Unit(id=1, start=10, end=12, text="At the turning point, velocity is zero."),
                  Unit(id=2, start=12, end=20, text="Therefore the answer is zero; gravity still acts."),
                  Unit(id=3, start=20, end=30, text="Please fill the homework poll.")]
        mapped = [activity(0, 3, ActivityKind.QUESTIONS)]
        checked = lessons.validate_study_edits(LessonEdits(activities=[edit(0, [cut(3, 3)], [0, 1, 2])]), mapped, source)
        plans, _ = plan_activities(source, lessons.make_labels(source, mapped, checked), 30, padding=0)
        self.assertEqual(intervals(plans["questions"]), [(0, 20)])
        self.assertEqual(plans["theory"], [])

    def test_concept_and_its_illustrative_example_stay_together_in_theory(self):
        source = units(6)
        source[0].text = "Acceleration means change of velocity per unit time."
        source[1].text = "For example, a speed increase of 2 m/s each second illustrates constant acceleration."
        mapped = [activity(0, 2), activity(3, 5, ActivityKind.QUESTIONS)]
        checked = lessons.validate_study_edits(LessonEdits(activities=[edit(0, essential=[0, 1, 2]),
            edit(3, essential=[3, 4, 5])]), mapped, source)
        plans, _ = plan_activities(source, lessons.make_labels(source, mapped, checked), 60, padding=0)
        self.assertEqual(intervals(plans["theory"]), [(0, 30)])
        self.assertEqual(intervals(plans["questions"]), [(30, 60)])

    def test_complete_short_solution_parts_survive_removal_of_intervening_chat(self):
        source = [Unit(id=0, start=0, end=2, text="The speed is zero at the top."),
                  Unit(id=1, start=2, end=10, text="Please vote in the attendance poll."),
                  Unit(id=2, start=10, end=12, text="But acceleration remains downward.")]
        mapped = [activity(0, 2, ActivityKind.QUESTIONS)]
        reviewed = [edit(0, [cut(1, 1)], [0, 2], subactivities=[
            activity(0, 0, ActivityKind.QUESTIONS), activity(1, 1, ActivityKind.NONACADEMIC),
            activity(2, 2, ActivityKind.QUESTIONS, depends_on=[0])])]
        lessons.validate_study_edits(LessonEdits(activities=reviewed), mapped, source)
        refined, edits = lessons.refine_activities(source, mapped, reviewed)
        plans, _ = plan_activities(source, lessons.make_labels(source, refined, edits), 12, padding=0)
        self.assertEqual(intervals(plans["questions"]), [(0, 2), (10, 12)])
        self.assertEqual(plans["theory"], [])

    def test_visual_review_flag_does_not_protect_clear_unrelated_passages(self):
        source = units(5)
        source[1].text = "[No reliable speech transcript; possible board work.]"
        source[3].text = "A separate unrelated scheduling announcement."
        mapped = [activity(0, 4, needs_visual_review=True)]
        edited = LessonEdits(activities=[edit(0, [cut(1, 1, Retention.WAITING), cut(3, 3, Retention.LOGISTICS)])])
        checked = lessons.validate_edits(edited, mapped, source)
        self.assertEqual([(c.first_id, c.last_id) for c in checked[0].cuts], [(3, 3)])
        labels = lessons.make_labels(source, mapped, checked)
        planned, _ = plan_activities(source, labels, 50, padding=0)
        self.assertEqual(intervals(planned["theory"]), [(0, 30), (40, 50)])


class LessonPlanningTests(unittest.TestCase):
    def test_dependency_closure_is_per_output_and_never_restores_cuts(self):
        source = units(8)
        mapped = [activity(0, 2), activity(3, 4, ActivityKind.QUESTIONS, depends_on=[0]),
                  activity(5, 5, depends_on=[3]), activity(6, 7, ActivityKind.NONACADEMIC)]
        edits = [edit(0, [cut(1, 1)], [0, 2]), edit(3, essential=[3, 4]),
                 edit(5, essential=[5]), edit(6, [cut(6, 7)])]
        labels = lessons.make_labels(source, mapped, edits)
        planned, report = plan_activities(source, labels, 80, padding=0)
        self.assertEqual(report["questions"]["required_context_activity_ids"], [0])
        self.assertEqual(report["theory"]["required_context_activity_ids"], [3])
        self.assertEqual(intervals(planned["questions"]), [(0, 10), (20, 50)])
        self.assertEqual(intervals(planned["theory"]), [(0, 10), (20, 60)])
        for output in planned.values():
            self.assertFalse(any(r.start < 20 and r.end > 10 for r in output))
            self.assertTrue(all(r.end <= 60 for r in output))

    def test_varied_subject_fixtures_preserve_complete_learning_activity(self):
        fixtures = {
            "Hinglish physics": ["Top par velocity zero hai.", "Acceleration -g hi hai, kyunki gravity acts.", "Dono quantities alag hain."],
            "history": ["The historian describes the revolt.", "This narrative explains its social causes.", "Those causes shaped the outcome."],
            "literature": ["Read the stanza aloud.", "Its repeated phrase builds the theme.", "The repetition expresses grief."],
            "biology": ["Draw the cell membrane.", "Label the channels and their orientation.", "Use the drawing to explain transport."],
            "coding": ["Given this list, implement binary search.", "Maintain the interval invariant.", "Return the matching index or minus one."],
        }
        for subject, texts in fixtures.items():
            with self.subTest(subject=subject):
                source = [Unit(id=i, start=i*10, end=(i+1)*10, text=text) for i, text in enumerate(texts)]
                kind = ActivityKind.QUESTIONS if subject == "coding" else ActivityKind.THEORY
                mapped = [activity(0, 2, kind)]
                labels = lessons.make_labels(source, mapped, [edit(0, essential=[0, 1, 2])])
                planned, _ = plan_activities(source, labels, 30, padding=0)
                self.assertEqual(intervals(planned[kind.value]), [(0, 30)])
                self.assertEqual(intervals(planned["combined"]), [(0, 30)])

    def test_long_untranscribed_board_work_is_retained_with_context(self):
        source = [Unit(id=0, start=0, end=10, text="Observe the diagram."),
                  Unit(id=1, start=10, end=70, text="[No reliable speech; possible silent board work.]"),
                  Unit(id=2, start=70, end=80, text="Now the labels explain the mechanism.")]
        mapped = [activity(0, 2, needs_visual_review=True)]
        checked = lessons.validate_edits(LessonEdits(activities=[edit(0, [cut(1, 1, Retention.WAITING)])]),
                                         mapped, source)
        labels = lessons.make_labels(source, mapped, checked)
        planned, _ = plan_activities(source, labels, 80, padding=0)
        self.assertEqual(intervals(planned["theory"]), [(0, 80)])

    def test_edge_padding_does_not_restore_interruption(self):
        source = units(5)
        mapped = [activity(0, 4)]
        edits = [edit(0, [cut(0, 0), cut(4, 4)], [1, 2, 3])]
        labels = lessons.make_labels(source, mapped, edits)
        planned, _ = plan_activities(source, labels, 50, padding=4)
        self.assertEqual(intervals(planned["combined"]), [(10, 40)])


class LessonRefinementTests(unittest.TestCase):
    def test_general_derivation_before_new_task_can_be_separated_without_loss(self):
        source = units(6)
        source[0].text = "For a dropped object, derive the general impact-speed formula."
        source[1].text = "The sign convention follows our chosen coordinate direction."
        source[2].text = "Now a new task: thrown downward at a specified speed from a given height."
        mapped = [activity(0, 5, ActivityKind.QUESTIONS)]
        reviewed = [edit(0, essential=[0, 1, 2, 5], subactivities=[
            activity(0, 1), activity(2, 5, ActivityKind.QUESTIONS, depends_on=[0])])]
        refined, edits = lessons.refine_activities(source, mapped, reviewed)
        labels = lessons.make_labels(source, refined, edits)
        plans, context = plan_activities(source, labels, 60, padding=0)
        self.assertEqual(intervals(plans["theory"]), [(0, 20)])
        self.assertEqual(intervals(plans["questions"]), [(0, 60)])
        self.assertEqual(context["questions"]["required_context_activity_ids"], [0])
        self.assertEqual({uid for child in edits for uid in child.essential_ids}, {0, 1, 2, 5})

    def test_same_exercise_recap_can_correct_category_without_importing_problem_into_theory(self):
        source = units(6)
        source[3].text = "Summarize this same example on a larger diagram."
        source[4].text = "Check the earlier result at the symmetric time."
        source[5].text = "This completes all parts of our one example."
        mapped = [activity(0, 2, ActivityKind.QUESTIONS), activity(3, 5, depends_on=[0])]
        reviewed = [edit(0), edit(3, subactivities=[activity(3, 5, ActivityKind.QUESTIONS, depends_on=[0])])]
        refined, edits = lessons.refine_activities(source, mapped, reviewed)
        labels = lessons.make_labels(source, refined, edits)
        plans, _ = plan_activities(source, labels, 60, padding=0)
        self.assertEqual(plans["theory"], [])
        self.assertEqual(intervals(plans["questions"]), [(0, 60)])

    def test_refinement_partitions_approved_cuts_and_essentials_exactly(self):
        source = units(5)
        mapped = [activity(0, 4)]
        reviewed = [edit(0, [cut(1, 3)], [0, 4], subactivities=[activity(0, 2), activity(3, 4)])]
        refined, edits = lessons.refine_activities(source, mapped, reviewed)
        self.assertEqual([(c.first_id, c.last_id) for e in edits for c in e.cuts], [(1, 2), (3, 3)])
        self.assertEqual([e.essential_ids for e in edits], [[0], [4]])
        plans, _ = plan_activities(source, lessons.make_labels(source, refined, edits), 50, padding=0)
        self.assertEqual(intervals(plans["combined"]), [(0, 10), (40, 50)])

    def test_bad_refinement_coverage_and_dependencies_fail_before_publication(self):
        source = units(4)
        mapped = [activity(0, 3)]
        for children in ([activity(0, 1), activity(3, 3)],
                         [activity(0, 2), activity(2, 3)],
                         [activity(0, 1), activity(2, 4)],
                         [activity(0, 1, depends_on=[2]), activity(2, 3)],
                         [activity(0, 1), activity(2, 3, depends_on=[1])]):
            with self.subTest(children=children), self.assertRaises(ValueError):
                lessons.refine_activities(source, mapped, [edit(0, subactivities=children)])

    def test_external_parent_dependency_preserves_all_refined_academic_context(self):
        source = units(6)
        mapped = [activity(0, 3), activity(4, 5, depends_on=[0])]
        reviewed = [edit(0, subactivities=[activity(0, 1), activity(2, 3, ActivityKind.QUESTIONS)]), edit(4)]
        refined, edits = lessons.refine_activities(source, mapped, reviewed)
        self.assertEqual(refined[-1].depends_on, [0, 2])
        plans, context = plan_activities(source, lessons.make_labels(source, refined, edits), 60, padding=0)
        self.assertEqual(intervals(plans["theory"]), [(0, 60)])
        self.assertEqual(context["theory"]["required_context_activity_ids"], [2])

    def test_refinement_cannot_delete_another_activitys_required_context(self):
        source = units(4)
        mapped = [activity(0, 1), activity(2, 3, depends_on=[0])]
        reviewed = [edit(0, subactivities=[activity(0, 1, ActivityKind.NONACADEMIC)]), edit(2)]
        with self.assertRaisesRegex(ValueError, "required by another"):
            lessons.refine_activities(source, mapped, reviewed)

    def test_refinement_cannot_restore_an_approved_cut(self):
        source = units(3)
        mapped = [activity(0, 2)]
        reviewed = [edit(0, [cut(1, 1)], [0, 2], subactivities=[activity(0, 0), activity(1, 1), activity(2, 2)])]
        with self.assertRaisesRegex(lessons.StudyReviewRequired, "delete all mapped teaching"):
            lessons.refine_activities(source, mapped, reviewed)

    def test_empty_subactivities_keeps_whole_legacy_activity(self):
        source = units(3)
        mapped = [activity(0, 2)]
        reviewed = LessonEdits.model_validate({"activities": [{"activity_id": 0, "cuts": [],
            "essential_ids": [0, 2], "review_required": False, "review_reason": ""}]})
        refined, edits = lessons.refine_activities(source, mapped, reviewed.activities)
        self.assertEqual(refined, mapped)
        self.assertEqual(edits[0].essential_ids, [0, 2])


class LessonRequestTests(unittest.TestCase):
    def test_uncut_poll_is_corrected_before_any_complete_result_is_cached(self):
        source = units(3)
        source[0].text = "Who submitted the homework? Vote in the poll."
        mapped = LessonMap(activities=[activity(0, 0, ActivityKind.NONACADEMIC), activity(1, 2)])
        bad = LessonEdits(activities=[edit(0), edit(1)])
        good = LessonEdits(activities=[edit(0, [cut(0, 0)]), edit(1, essential=[1, 2])])
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as directory, \
             patch.object(config, "TEMP_DIR", Path(directory)), \
             patch("google.genai.Client", return_value=MagicMock()), \
             patch.object(classifier, "_request", side_effect=[mapped, bad, good]) as request:
            labels = lessons.classify_activities(source, backend="gemini")
            self.assertEqual(request.call_count, 3)
            self.assertIn("nonacademic IDs", request.call_args_list[-1].args[2])
            cached = lessons.classify_activities(source, backend="gemini")
            self.assertEqual(request.call_count, 3)
            plans, _ = plan_activities(source, cached, 30, padding=0)
            self.assertEqual(intervals(plans["combined"]), [(10, 30)])
            self.assertEqual(labels, cached)

    def test_malformed_map_is_retried_then_complete_result_is_cached(self):
        source = units(3)
        bad = LessonMap(activities=[activity(0, 1)])
        good = LessonMap(activities=[activity(0, 2)])
        reviewed = LessonEdits(activities=[edit(0, essential=[0, 2])])
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as directory, \
             patch.object(config, "TEMP_DIR", Path(directory)), \
             patch("google.genai.Client", return_value=MagicMock()) as client, \
             patch.object(classifier, "_request", side_effect=[bad, good, reviewed]) as request:
            first = lessons.classify_activities(source, backend="gemini")
            cached = lessons.classify_activities(source, backend="gemini")
            self.assertEqual(set(first), {0, 1, 2})
            self.assertEqual(first, cached)
            self.assertEqual(request.call_count, 3)
            self.assertIn("Correct the invalid response", request.call_args_list[1].args[2])
            self.assertEqual(client.call_count, 1)

    def test_failed_map_retry_does_not_publish_or_poison_complete_cache(self):
        source = units(3)
        bad = LessonMap(activities=[activity(0, 1)])
        good = LessonMap(activities=[activity(0, 2)])
        reviewed = LessonEdits(activities=[edit(0)])
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as directory, \
             patch.object(config, "TEMP_DIR", Path(directory)), \
             patch("google.genai.Client", return_value=MagicMock()), \
             patch.object(classifier, "_request", side_effect=[bad, bad, good, reviewed]) as request:
            with self.assertRaises(ValueError):
                lessons.classify_activities(source, backend="gemini")
            self.assertEqual(list(Path(directory).glob("lessons_*.json")), [])
            recovered = lessons.classify_activities(source, backend="gemini")
            self.assertEqual(set(recovered), {0, 1, 2})
            self.assertEqual(request.call_count, 4)

    def test_cross_window_continuation_preserves_new_dependencies(self):
        source = units(1402, seconds=1)
        map_calls = 0
        def respond(client, backend, prompt, system, schema):
            nonlocal map_calls
            if schema is LessonMap:
                map_calls += 1
                if map_calls == 1:
                    return LessonMap(activities=[activity(0, 1), activity(2, 1399, ActivityKind.QUESTIONS)])
                return LessonMap(activities=[
                    activity(1400, 1400, ActivityKind.QUESTIONS, continues_previous=True, depends_on=[0, 2]),
                    activity(1401, 1401, depends_on=[1400]),
                ])
            data = json.loads(prompt.split("Activities to review:\n", 1)[1].split("\nFor an activity", 1)[0])
            return LessonEdits(activities=[edit(item["first_id"]) for item in data])
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as directory, \
             patch.object(config, "TEMP_DIR", Path(directory)), \
             patch("google.genai.Client", return_value=MagicMock()), \
             patch.object(classifier, "_request", side_effect=respond):
            with patch.object(lessons, "MAP_WINDOW_UNITS", 1400):
                result = lessons.classify_activities(source, backend="gemini")
            self.assertEqual([(a.first_id, a.last_id) for a in result.activities], [(0, 1), (2, 1400), (1401, 1401)])
            self.assertEqual(result.activities[1].depends_on, [0])
            self.assertEqual(result.activities[2].depends_on, [2])
            planned, report = plan_activities(source, result, 1402, padding=0)
            self.assertEqual(intervals(planned["questions"]), [(0, 1401)])
            self.assertEqual(report["questions"]["required_context_activity_ids"], [0])

    def test_retry_reuses_validated_map_and_successful_edit_batches(self):
        source = units(200, seconds=1)
        mapped = LessonMap(activities=[activity(0, 99), activity(100, 199)])
        requests = {"map": 0, 0: 0, 100: 0}
        fail_second_batch = True
        def respond(client, backend, prompt, system, schema):
            if schema is LessonMap:
                requests["map"] += 1
                return mapped
            data = json.loads(prompt.split("Activities to review:\n", 1)[1].split("\nFor an activity", 1)[0])
            aid = data[0]["first_id"]
            requests[aid] += 1
            if aid == 100 and fail_second_batch:
                return LessonEdits(activities=[])  # Invalid on both bounded attempts.
            return LessonEdits(activities=[edit(aid)])
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as directory, \
             patch.object(config, "TEMP_DIR", Path(directory)), \
             patch.object(config, "GEMINI_PARALLEL_REQUESTS", 1), \
             patch.object(lessons, "EDIT_BATCH_UNITS", 180), \
             patch("google.genai.Client", return_value=MagicMock()), \
             patch.object(classifier, "_request", side_effect=respond):
            with self.assertRaises(ValueError):
                lessons.classify_activities(source, backend="gemini")
            self.assertEqual(list(Path(directory).glob("lessons_*.json")), [])
            fail_second_batch = False
            result = lessons.classify_activities(source, backend="gemini")
            self.assertEqual(len(result), 200)
            self.assertEqual(requests, {"map": 1, 0: 1, 100: 3})


if __name__ == "__main__":
    unittest.main()
