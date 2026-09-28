# RET-C2-246 — Unit Tests: deterministic severity scoring & classification

from src.services.severity_service import (
    classify_type,
    classify_incidents,
    score_incident,
    score_incidents,
)


class TestClassification:
    def test_keyword_mapping(self):
        assert classify_type("wet floor slip risk") == "slip_zone"
        assert classify_type("fire hazard near shelf") == "fire_hazard"
        assert classify_type("blocked emergency exit") == "egress_obstruction"
        assert classify_type("crowd congestion at register") == "overcrowding"
        assert classify_type("shelf collapse") == "structural"

    def test_japanese_keywords(self):
        assert classify_type("床が濡れて滑る") == "slip_zone"
        assert classify_type("非常口が塞がれている") == "egress_obstruction"

    def test_unknown_is_other(self):
        assert classify_type("misc note") == "other"
        assert classify_type("") == "other"

    def test_classify_incidents_attaches_category(self):
        out = classify_incidents([{"type": "wet floor", "location": "A"}])
        assert out[0]["category"] == "slip_zone"
        assert out[0]["location"] == "A"  # original fields preserved


class TestScoring:
    def test_deterministic_same_input_same_output(self):
        inc = {"id": "x", "location": "A", "category": "slip_zone"}
        assert score_incident(inc) == score_incident(inc)

    def test_injury_raises_severity(self):
        base = score_incident({"category": "slip_zone"})
        injured = score_incident({"category": "slip_zone", "injury": True})
        assert injured["score"] > base["score"]

    def test_fire_with_injury_is_high_and_capped(self):
        s = score_incident({"category": "fire_hazard", "injury": True})
        assert s["severity"] == "high"
        assert s["score"] == 10  # capped at 10

    def test_people_affected_threshold(self):
        few = score_incident({"category": "overcrowding", "people_affected": 2})
        many = score_incident({"category": "overcrowding", "people_affected": 5})
        assert many["score"] > few["score"]

    def test_score_incidents_sorted_desc(self):
        scored = score_incidents(
            [
                {"category": "other", "location": "A"},
                {"category": "fire_hazard", "location": "B", "injury": True},
            ]
        )
        assert [s["score"] for s in scored] == sorted(
            [s["score"] for s in scored], reverse=True
        )
        assert scored[0]["location"] == "B"

    def test_severity_labels(self):
        assert score_incident({"category": "other"})["severity"] == "low"
        assert score_incident({"category": "slip_zone"})["severity"] == "medium"
        assert score_incident({"category": "fire_hazard"})["severity"] == "high"
