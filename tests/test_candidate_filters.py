"""Synthetic feature-policy fixtures; no real library/audio is used."""

from dataclasses import FrozenInstanceError, replace
import math

import pytest

from backend.contracts import AudioFeatures, AudioMetadata, MEASURES, Measurement, MusicalKey, Sample, SongContext
from backend.palette.compatibility import (Availability as A, FilterInputError, FilterPolicy,
                                           POLICY_VERSION, filter_candidates)


def measure(name, value=None, confidence=None):
    return Measurement(name=name,value=value,unit=MEASURES[name][0],confidence=confidence,
                       unavailable_reason="not_implemented" if value is None else None)


def key(tonic=None, mode="major", confidence=.8):
    return MusicalKey(tonic=tonic,mode=mode if tonic else None,confidence=confidence if tonic else None,
                      unavailable_reason=None if tonic else "insufficient_key_context")


def sample(identifier="bass-a",role="bass",peak=1.,rms=.2,frames=48000,tempo=None,tempo_confidence=None,
           tonal=None,analysis="synthetic-v1"):
    values={name:measure(name) for name in MEASURES}
    values.update(peak=measure("peak",peak),rms=measure("rms",rms),tempo=measure("tempo",tempo,tempo_confidence))
    return Sample(sample_id=identifier,role=role,
                  audio=AudioMetadata(local_path="C:/synthetic-policy-fixture.wav",sample_rate_hz=48000,
                                      channels=1,frame_count=frames,duration_ms=frames/48),
                  features=AudioFeatures(measurements=tuple(values.values()),key=tonal or key()),analysis_version=analysis)


def song(tempo=100.,confidence=.8,tonal=None):
    return SongContext(tempo=measure("tempo",tempo,confidence if tempo is not None else None),
                       key=tonal or key("C"),genre=None,genre_unavailable_reason="unknown")


def run(candidates,policy=None,context=None,availability=None,kick=None):
    kick=kick or sample("kick",role="kick")
    if availability is None:
        availability={s.sample_id:A.AVAILABLE for s in (kick,*candidates)}
    return filter_candidates(kick,candidates,policy=policy or FilterPolicy(),context=context,availability=availability)


def codes(result):
    return tuple(reason.code for entry in result.excluded for reason in entry.reasons)


def test_defaults_preserve_uncertainty_subbass_and_overrange():
    candidates=[sample("bass",peak=2.,rms=None),sample("sub",role="sub-bass",frames=1,peak=.01,rms=None)]
    result=run(candidates)
    assert result.eligible_ids==("bass","sub") and result.excluded==()
    assert result.policy==FilterPolicy(False,False) and result.policy_version==POLICY_VERSION


@pytest.mark.parametrize("state,reason",[(A.MISSING,"file_missing"),(A.UNREADABLE,"file_unreadable"),(A.UNKNOWN,"availability_unknown")])
def test_availability_states(state,reason):
    assert codes(run([sample()],availability={"kick":A.AVAILABLE,"bass-a":state}))==(reason,)


def test_missing_availability_is_unverified_not_available():
    assert codes(run([sample()],availability={"kick":A.AVAILABLE}))==("availability_unknown",)


def test_selected_kick_id_takes_role_precedence():
    assert codes(run([sample("kick",role="kick")]))==("selected_kick",)
    assert codes(run([sample("other",role="kick")]))==("wrong_role",)


@pytest.mark.parametrize("kwargs,expected",[
    ({"frames":0},("empty_audio",)),({"peak":0,"rms":0},("silent_audio",)),
    ({"peak":None},("unusable_analysis",)),({"peak":0,"rms":.1},("silent_audio","inconsistent_analysis")),
    ({"peak":.2,"rms":.3},("inconsistent_analysis",)),({"peak":1.,"rms":None},()),
    ({"peak":1e-300,"rms":1e-300},()),({"peak":1e300,"rms":1e300},()),
    ({"peak":1.,"rms":1.+5e-13},()),({"peak":1.,"rms":1.+2e-12},("inconsistent_analysis",))])
def test_minimal_core_and_roundoff(kwargs,expected):
    assert codes(run([sample(**kwargs)]))==expected


def test_multiple_reasons_have_fixed_order_and_supporting_facts():
    candidate=sample("other",role="kick",frames=0,peak=0,rms=.1,tempo=200,tempo_confidence=.9,tonal=key("D",confidence=.9))
    result=run([candidate],FilterPolicy(True,True),song(),{"kick":A.AVAILABLE,"other":A.MISSING})
    assert codes(result)==("wrong_role","file_missing","empty_audio","silent_audio","inconsistent_analysis","tempo_mismatch","key_mismatch")
    facts=dict(result.excluded[0].reasons[-2].facts)
    assert facts["candidate_bpm"]==200 and facts["song_bpm"]==100
    assert all(tuple(sorted(r.facts))==r.facts for r in result.excluded[0].reasons)


@pytest.mark.parametrize("bpm,excluded",[(math.nextafter(105.,0),False),(105.,False),(math.nextafter(105.,math.inf),True),
                                        (math.nextafter(95.,0),True),(95.,False),(math.nextafter(95.,math.inf),False),
                                        (50.,True),(200.,True)])
def test_tempo_five_percent_both_edges_without_octave_matching(bpm,excluded):
    result=run([sample(tempo=bpm,tempo_confidence=.8)],FilterPolicy(tempo_lock=True),song())
    assert ("tempo_mismatch" in codes(result))==excluded


@pytest.mark.parametrize("confidence,excluded",[(math.nextafter(.8,0),False),(.8,True),(math.nextafter(.8,1),True)])
@pytest.mark.parametrize("side",["candidate","song"])
def test_tempo_confidence_boundary(confidence,excluded,side):
    c=sample(tempo=200,tempo_confidence=confidence if side=="candidate" else .9)
    context=song(confidence=confidence if side=="song" else .9)
    assert ("tempo_mismatch" in codes(run([c],FilterPolicy(tempo_lock=True),context)))==excluded


@pytest.mark.parametrize("confidence,excluded",[(math.nextafter(.8,0),False),(.8,True),(math.nextafter(.8,1),True)])
@pytest.mark.parametrize("side",["candidate","song"])
def test_key_confidence_boundary(confidence,excluded,side):
    c=sample(tonal=key("D",confidence=confidence if side=="candidate" else .9))
    context=song(tonal=key("C",confidence=confidence if side=="song" else .9))
    assert ("key_mismatch" in codes(run([c],FilterPolicy(exact_key_lock=True),context)))==excluded


@pytest.mark.parametrize("tonic,mode,expected",[("C","major",()),("C","minor",("key_mismatch",)),("C#","major",("key_mismatch",))])
def test_exact_tonic_and_mode_lock(tonic,mode,expected):
    assert codes(run([sample(tonal=key(tonic,mode))],FilterPolicy(exact_key_lock=True),song()))==expected


def test_disabled_locks_unknown_context_and_current_unknown_tempo():
    conflicting=sample(tempo=200,tempo_confidence=.99,tonal=key("D",confidence=.99))
    assert run([conflicting],context=song()).eligible_ids==("bass-a",)
    assert run([conflicting],FilterPolicy(True,True)).eligible_ids==("bass-a",)
    unknown=SongContext(tempo=measure("tempo"),key=key(),genre="synthetic genre")
    assert run([conflicting],FilterPolicy(True,True),unknown).eligible_ids==("bass-a",)
    assert run([sample()],FilterPolicy(True,True),song()).eligible_ids==("bass-a",)


def test_fundamental_never_substitutes_for_key_or_context():
    candidate=sample()
    measures=tuple(measure("fundamental",110,.99) if m.name=="fundamental" else m for m in candidate.features.measurements)
    candidate=replace(candidate,features=replace(candidate.features,measurements=measures))
    assert run([candidate],FilterPolicy(True,True),song()).eligible_ids==("bass-a",)


@pytest.mark.parametrize("kick",[sample("kick",role="bass"),sample("kick",role="kick",frames=0),
    sample("kick",role="kick",peak=0,rms=0),sample("kick",role="kick",peak=None),
    sample("kick",role="kick",peak=.1,rms=.2)])
def test_invalid_kick_is_input_error(kick):
    with pytest.raises(FilterInputError) as error: run([],kick=kick)
    assert error.value.code=="invalid_kick"


@pytest.mark.parametrize("state",[None,A.MISSING,A.UNREADABLE,A.UNKNOWN])
def test_unverified_or_unavailable_kick_is_input_error(state):
    availability={} if state is None else {"kick":state}
    with pytest.raises(FilterInputError) as error: run([],availability=availability)
    assert error.value.code=="invalid_kick"


@pytest.mark.parametrize("availability",[None,[],{"kick":"available"},{"kick":A.AVAILABLE,"unrelated":A.AVAILABLE},{3:A.AVAILABLE}])
def test_inconsistent_availability_is_input_error(availability):
    with pytest.raises(FilterInputError) as error:
        filter_candidates(sample("kick",role="kick"),[],policy=FilterPolicy(),availability=availability)
    assert error.value.code=="invalid_availability"


def test_duplicate_malformed_inputs_policy_and_context():
    with pytest.raises(FilterInputError,match="unique"): run([sample(),sample()])
    for candidates in (None,"bass",[{}]):
        with pytest.raises(FilterInputError): filter_candidates(sample("kick",role="kick"),candidates,policy=FilterPolicy(),availability={"kick":A.AVAILABLE})
    with pytest.raises(FilterInputError): FilterPolicy(tempo_lock=1)
    with pytest.raises(FilterInputError): run([],context={})
    broken=sample()
    object.__setattr__(broken,"role","not-a-role")
    with pytest.raises(FilterInputError): run([broken])


def test_sorted_stateless_results_versions_and_immutable_inputs():
    candidates=[sample("z",analysis="different-v2"),sample("a",peak=None,analysis="older-v1"),sample("m",role="sub-bass")]
    before=[s.to_json() for s in candidates]
    first=run(candidates)
    run([sample("other")],FilterPolicy(True,True),song())
    second=run(list(reversed(candidates)))
    assert first==second
    assert first.eligible_ids==("m","z")
    assert first.analysis_versions==(("a","older-v1"),("m","synthetic-v1"),("z","different-v2"))
    assert first.excluded[0].analysis_version=="older-v1"
    assert [s.to_json() for s in candidates]==before
    with pytest.raises(FrozenInstanceError): first.eligible_ids=()


def test_empty_all_excluded_and_extreme_tempo_have_finite_facts():
    assert run([]).eligible_ids==() and run([]).excluded==()
    assert run([sample(peak=None)]).eligible_ids==()
    result=run([sample(tempo=1e308,tempo_confidence=1)],FilterPolicy(tempo_lock=True),song(tempo=1e-300,confidence=1))
    assert codes(result)==("tempo_mismatch",)
    assert all(not isinstance(v,float) or math.isfinite(v) for _,v in result.excluded[0].reasons[0].facts)
