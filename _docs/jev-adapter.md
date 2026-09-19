# Bounded TypeSafe Jev transport adapter

`backend/intelligence/jev.py` is the one bounded adapter between the validated #13 contract
(`backend/intelligence/questions.py`, `backend/intelligence/decisions.py`) and the
TypeSafe Jev interface, and `backend/intelligence/jev_double.py` is the deterministic contract
double the adapter is proved against. The adapter scores the questions it is given: it never builds a
question, never chooses a dimension, never measures audio and never opens a file. DSP owns measured
facts, Jev supplies typed judgments, and application code owns final ranking
([#15](https://github.com/gmphto/tera/issues/15)). Phase 0 work; [#14](https://github.com/gmphto/tera/issues/14).

## Module map and entry point

| Module | Role |
| --- | --- |
| `backend/intelligence/jev.py` | `score_questions`, the bounds, the records, `HttpJevTransport` and `NoRedirectHandler` |
| `backend/intelligence/jev_double.py` | `JevContractDouble`, the deterministic contract double that implements `JevTransport` |
| `backend/intelligence/questions.py` | The #13 question payload; imported, never re-declared |
| `backend/intelligence/decisions.py` | The #13 response validation; imported, never re-declared |
| `backend/contracts.py` | `JevJudgment`, recorded unchanged; schema 1.0, never modified here |

One entry point:

```python
from backend.intelligence.jev import JevScoringRequest, score_questions

run = score_questions(
    [JevScoringRequest(request_id, question)],
    transport=None,     # or any JevTransport; a double is never reached implicitly
    config=None,        # a JevAdapterConfig, or the published defaults
    credentials=None,   # a JevCredentials, or TERA_JEV_ENDPOINT and TERA_JEV_API_KEY
    environ=None,       # None means the process environment
    cancelled=None,     # an optional zero-argument callable
    sleep=None,         # the injected wait callable, default time.sleep
    monotonic=None,     # the injected clock, default time.monotonic
)
```

`score_questions` returns one `JevScoringRun` and raises `JevAdapterError`
only before anything is sent. `JevContractDouble(model_version, default, script)` implements
`JevTransport` with `interface_name="jev-contract-double"` and
`source="double"`.

`jev.py` imports only the standard library (`json`, `os`, `time`,
`urllib.error`, `urllib.request`, `dataclasses`), `backend.contracts`,
`backend.intelligence.questions` and `backend.intelligence.decisions`. It never
imports `backend.intelligence.jev_double`, so a double can never be reached implicitly. It
contains no reference to `local_path`, `frame_count`, `backend.audio`,
`backend.analysis`, `soundfile` or audio decoding, and it never calls the built-in
file opener. Neither module changes the behaviour of any existing module.

## Constants

| Constant | Value | Meaning |
| --- | --- | --- |
| `ADAPTER_VERSION` | `"jev-adapter-v1"` | The version of the record this adapter writes for #15 and #19 |
| `DEFAULT_TIMEOUT_S` | `20.0` | One send's timeout when a configuration does not override it |
| `MIN_TIMEOUT_S` | `1.0` | The smallest configured timeout |
| `MAX_TIMEOUT_S` | `120.0` | The hard ceiling on one send's configured timeout |
| `DEFAULT_MAX_ATTEMPTS` | `3` | Sends attempted for one question by default |
| `MIN_MAX_ATTEMPTS` | `1` | The fewest configured attempts |
| `MAX_MAX_ATTEMPTS` | `5` | The most configured attempts; retrying stops here |
| `DEFAULT_BACKOFF_S` | `0.5` | The first backoff wait |
| `BACKOFF_MULTIPLIER` | `2.0` | The factor between two backoff waits |
| `MAX_BACKOFF_S` | `8.0` | The default clamp on one backoff wait |
| `DEFAULT_MAX_BATCH_SECONDS` | `120.0` | One call's wall-clock budget by default |
| `MIN_MAX_BATCH_SECONDS` | `1.0` | The smallest configured batch budget |
| `MAX_MAX_BATCH_SECONDS` | `600.0` | The largest configured batch budget |
| `MAX_BATCH_SIZE` | `100` | The hard ceiling on questions per call; the caller issues bounded calls |
| `OUTCOME_STATES` | `("judged", "abstained", "not_asked", "not_attempted", "invalid_result", "service_error", "timed_out", "unavailable")` | Every state one outcome can carry, in documented order |
| `TRANSPORT_ERROR_CODES` | `("connection_failed", "service_unavailable", "service_error", "rate_limited", "timeout", "invalid_credentials")` | Every code a transport failure can carry |
| `RETRYABLE_TRANSPORT_CODES` | `("connection_failed", "service_unavailable", "service_error", "rate_limited", "timeout")` | The transport codes retried while attempts and budget remain |
| `NOT_ATTEMPTED_CODES` | `("cancelled", "batch_deadline_exceeded")` | The codes for a request the run never sent |
| `UNAVAILABLE_CODES` | `frozenset({"credentials_absent"})` | The documented frozenset for a request that was never reachable |
| `ADAPTER_ERROR_CODES` | `("invalid_configuration", "batch_too_large", "duplicate_request_id", "invalid_requests")` | Every code a JevAdapterError can carry; all are raised before any send |
| `TRANSPORT_SOURCES` | `("interface", "double")` | The transport sources the adapter accepts and records |
| `ENV_ENDPOINT` | `"TERA_JEV_ENDPOINT"` | The endpoint variable JevCredentials.from_env reads |
| `ENV_API_KEY` | `"TERA_JEV_API_KEY"` | The key variable JevCredentials.from_env reads |
| `ENV_TIMEOUT_S` | `"TERA_JEV_TIMEOUT_S"` | The timeout override JevAdapterConfig.from_env reads |
| `ENV_MAX_ATTEMPTS` | `"TERA_JEV_MAX_ATTEMPTS"` | The attempts override JevAdapterConfig.from_env reads |
| `ENV_MAX_BATCH_SECONDS` | `"TERA_JEV_MAX_BATCH_SECONDS"` | The batch budget override JevAdapterConfig.from_env reads |
| `PROMPT_VERSION` | `"jev-questions-v1"` | The #13 prompt version every run records, imported and never re-declared |
| `DOUBLE_INTERFACE_NAME` | `"jev-contract-double"` | The interface name every double outcome carries |
| `DOUBLE_SOURCE` | `"double"` | The record source every double outcome carries |
| `DEFAULT_MODEL_VERSION` | `"synthetic-jev-double-v1"` | The double's synthetic model version unless a script overrides it |
| `DEFAULT_PROBABILITIES` | `(0.05, 0.05, 0.1, 0.6, 0.2)` | The fixed valid judgment the double falls back to |
| `DEFAULT_CONFIDENCE` | `0.6` | The confidence of that fixed fallback judgment |
| `ENTRY_KINDS` | `("judgment", "abstain", "text", "error")` | The four script entry kinds; an entry carries exactly one |

## Bounds

| Field | Default | Minimum | Maximum |
| --- | --- | --- | --- |
| `timeout_s` | `20.0` | `1.0` | `120.0` |
| `max_attempts` | `3` | `1` | `5` |
| `backoff_s` | `0.5` | `0 (exclusive)` | `unbounded, finite` |
| `max_backoff_s` | `8.0` | `0 (exclusive)` | `unbounded, finite` |
| `max_batch_seconds` | `120.0` | `1.0` | `600.0` |
| `max_batch_size` | `100` | `1` | `100` |

Every bound is inclusive. A value outside its bound, a non-finite or non-positive value, or a
wrong type raises `JevAdapterError` with `.code == "invalid_configuration"`
before anything is sent, so a rejected configuration produces no request, no outcome and no partial
run. `backoff_s` and `max_backoff_s` have no published upper bound: they only
have to be positive and finite, and one wait is clamped by `max_backoff_s`.
`max_batch_size` may never exceed `MAX_BATCH_SIZE`, so one call never sends more
than `MAX_BATCH_SIZE` questions per call.

## Environment variables

| Variable | Read by | Meaning |
| --- | --- | --- |
| `TERA_JEV_ENDPOINT` | `JevCredentials.from_env` | The one destination the adapter may POST to; https anywhere, or plain http only on localhost/127.0.0.1 |
| `TERA_JEV_API_KEY` | `JevCredentials.from_env` | The bearer credential; rendered only as `<redacted>` |
| `TERA_JEV_TIMEOUT_S` | `JevAdapterConfig.from_env` | Overrides `timeout_s`; bounds `MIN_TIMEOUT_S` to `MAX_TIMEOUT_S` |
| `TERA_JEV_MAX_ATTEMPTS` | `JevAdapterConfig.from_env` | Overrides `max_attempts`; bounds `MIN_MAX_ATTEMPTS` to `MAX_MAX_ATTEMPTS` |
| `TERA_JEV_MAX_BATCH_SECONDS` | `JevAdapterConfig.from_env` | Overrides `max_batch_seconds`; bounds `MIN_MAX_BATCH_SECONDS` to `MAX_MAX_BATCH_SECONDS` |

`environ=None` means the process environment. A missing variable keeps its default; a
present but blank, non-numeric or out-of-bounds value raises `JevAdapterError` with
`.code == "invalid_configuration"` and a message that names the variable and its allowed
bounds and never echoes the supplied value. Credentials follow the same blank rule: unless both
variables are non-blank, `from_env` returns `None` and the run is an
unavailable outcome set.

## Credentials, redaction and the endpoint rule

`JevCredentials(endpoint, api_key)` reads `TERA_JEV_ENDPOINT` and
`TERA_JEV_API_KEY`; both values are trimmed, and a blank value counts as absent.
`from_env` returns `None` unless both are non-blank.

The endpoint must be an absolute `https://` URL, or an `http://` URL whose host is
`localhost` or `127.0.0.1`. Every other endpoint — another scheme, a relative
URL, a non-loopback `http` host — is `invalid_configuration` before anything is
sent, so the payload can never travel in the clear off the device. The value itself is never echoed
in a message.

`__repr__` and `__str__` render both values as `<redacted>`. Every
`JevAdapterError` and `JevTransportError` message, every call log entry and every
`to_json()` record render a credential at most as a presence flag: no message, log or
record ever prints the key or the endpoint. The only place the key appears is the outgoing
`Authorization: Bearer <api_key>` header of the assumed HTTP mapping; the payload is the
only data the adapter sends and the endpoint is the only destination.

## The assumed HTTP wire mapping

**This mapping is assumed.** The real TypeSafe Jev interface and credentials are not available in
this environment, so the mapping below is a hypothesis that a later credentialed run either
confirms or replaces; nothing in this repository has observed it. It stays marked assumed until such
a run reports otherwise.

`HttpJevTransport(credentials, *, opener=None)` declares
`interface_name="typesafe-jev-http"` and `source="interface"` and:

* POSTs `question.to_json()` bytes, unchanged and with no added field, to the configured
  endpoint;
* sends `Content-Type: application/json` and `Authorization: Bearer <api_key>`;
* passes `timeout=timeout_s` for that attempt;
* returns a 2xx response body as text for #13 to validate or reject.

| Failure | Transport code and state |
| --- | --- |
| 401, 403 | `invalid_credentials`, then `service_error` (never retried) |
| 429 | `rate_limited`, then `service_error` |
| 503, 504 | `service_unavailable`, then `service_error` |
| 3xx and every other 4xx or 5xx | `service_error` |
| `TimeoutError`, including one a `URLError` wraps | `timeout`, then `timed_out` |
| another `URLError` or `OSError` | `connection_failed` |
| a 2xx body that is not valid UTF-8 | `service_error` |

The default opener is built with `NoRedirectHandler`, which refuses every redirect, so a
redirect can never move the payload to another host; a refused redirect surfaces as a 3xx
`service_error`. The local proof injects a recording fake opener, so no test opens a
socket, contacts a service or needs the network — which is what makes the local verification
independent of credentials.

## Timeouts, retries and the batch deadline

The adapter never blocks on its own. Before every send it reads the injected clock and passes
`timeout_s = min(config.timeout_s, remaining batch seconds)` to that `send`
call, so a send near the deadline receives the reduced remaining-budget value. The enforced
worst-case wall time for one call is `max_batch_seconds + timeout_s` — 140 s with the
defaults — and `MAX_TIMEOUT_S = 120.0` is the hard ceiling the adapter never exceeds. In
practice each send's timeout is additionally capped at the remaining budget, so a call cannot
outlast `max_batch_seconds` from its own start.

Only `RETRYABLE_TRANSPORT_CODES` are retried. `invalid_credentials` and every
`invalid_result` are not, and retrying stops at `max_attempts`. The wait before
attempt n+1 is

```text
min(config.backoff_s * BACKOFF_MULTIPLIER ** (n - 1), config.max_backoff_s)
```

which is 0.5 s then 1.0 s with the defaults. It is never applied after the final attempt, uses no
jitter, no randomness and no wall-clock call, and goes through the injected `sleep`
callable, so a test records the exact delay sequence without waiting. The final outcome reflects
the last attempt: a timeout last gives `timed_out` with `code="timeout"`, and
any other transport failure last gives `service_error` with that code. A transport code
outside `TRANSPORT_ERROR_CODES` is recorded as `service_error` with
`code="service_error"`.

Before every send the adapter also compares the elapsed time against
`config.max_batch_seconds`. When the budget is gone it starts no further send: that
request and every remaining one carry `state="not_attempted"`,
`code="batch_deadline_exceeded"` and the attempts already made, so a whole call is
bounded.

## Cancellation

`cancelled` is an optional zero-argument callable returning truthy to stop. It is
consulted before the first send, before every retry, before every sleep and before every subsequent
request. Once it returns truthy no further send or sleep happens, every request already answered
keeps its outcome unchanged, and every remaining request — including one whose question is an
`UnavailableQuestion` and one interrupted between attempts — is recorded as
`state="not_attempted"`, `code="cancelled"` with the number of sends it already
made. Cancellation is not an exception, it never discards a completed outcome, and
`run.cancelled` is then `true`.

## Outcome states

| State | Code space | Meaning |
| --- | --- | --- |
| `judged` | JevJudgment | The interface returned a response #13 accepted with a label; the JevJudgment is recorded unchanged |
| `abstained` | JevJudgment | The interface returned the model's own model_abstained abstention; the JevJudgment carries a null label and no probabilities |
| `not_asked` | `QUESTION_UNAVAILABLE_CODES` | This product never asked the question, so there was nothing to send; code is copied verbatim from the UnavailableQuestion |
| `not_attempted` | `NOT_ATTEMPTED_CODES` | The run stopped before this request: it was cancelled or the batch deadline was gone |
| `invalid_result` | `RESPONSE_ERROR_CODES` | A response came back and #13 rejected it; it is terminal, verbatim and never retried |
| `service_error` | `TRANSPORT_ERROR_CODES` other than timeout | The transport failed for a reason other than a timeout, after the attempts were exhausted |
| `timed_out` | `timeout` | The last attempt reported a timeout after the attempts were exhausted |
| `unavailable` | `UNAVAILABLE_CODES` | No credentials were configured, so no transport was constructed and nothing was sent |

Every invariant below is enforced by `JevOutcome` itself, so a hand-built record cannot
disagree with its code space: `judged` and `abstained` carry a
`JevJudgment` and `code=None`; `not_asked` carries a #13 question
code and `attempts=0`; `not_attempted` carries a
`NOT_ATTEMPTED_CODES` code; `invalid_result` carries a #13 response code;
`service_error` carries a transport code other than `timeout`;
`timed_out` carries `code="timeout"`; `unavailable` carries an
`UNAVAILABLE_CODES` code. `attempts` is always the exact number of sends made
for that request: 0 for a request that was never started and 1 or more for one interrupted between
attempts. The only name that is both a state and a code is `service_error`, deliberately.

## Transport error codes

| Code | State | Meaning |
| --- | --- | --- |
| `connection_failed` | `service_error` | The connection could not be opened or the response body could not be read: another URLError or OSError, or a reset |
| `service_unavailable` | `service_error` | HTTP 503 or 504 after the attempts were exhausted |
| `service_error` | `service_error` | A 3xx, another 4xx or 5xx, an out-of-contract transport code, or a response body that is not UTF-8 text |
| `rate_limited` | `service_error` | HTTP 429 after the attempts were exhausted |
| `timeout` | `timed_out` | A TimeoutError, including one a URLError wraps, reported on the last attempt |
| `invalid_credentials` | `service_error` | HTTP 401 or 403; never retried |

`RETRYABLE_TRANSPORT_CODES` is the tuple of the five retryable entries in the order
above; `invalid_credentials` is the sixth and is never retried.

## Not-attempted codes

| Code | State | Meaning |
| --- | --- | --- |
| `cancelled` | `not_attempted` | The caller's cancelled callable returned truthy before this request was sent |
| `batch_deadline_exceeded` | `not_attempted` | The elapsed time reached max_batch_seconds before this send started |

## Unavailable codes

| Code | State | Meaning |
| --- | --- | --- |
| `credentials_absent` | `unavailable` | Neither a transport, nor credentials, nor TERA_JEV_ENDPOINT and TERA_JEV_API_KEY were supplied, so the interface was never reached |

## Adapter error codes

| Code | Outcome state | Meaning |
| --- | --- | --- |
| `invalid_configuration` | Raised during setup, before any send | A configuration value, a transport, an injected callable or a run record outside this contract; the environment override message names the variable and its bounds and never echoes the supplied value |
| `batch_too_large` | Raised before any send | The sequence holds more than max_batch_size questions; at most MAX_BATCH_SIZE are sent per call |
| `duplicate_request_id` | Raised before any send | Two requests in one batch share one request_id, so their outcomes could not be told apart |
| `invalid_requests` | Raised before any send | requests is not a list or tuple, an entry is not a JevScoringRequest, a request_id is blank or missing, or a question is not a #13 JevQuestion or UnavailableQuestion |

An adapter error produces no request, no outcome and no partial `JevScoringRun`.

## Question-unavailable codes

| Code | State | Meaning |
| --- | --- | --- |
| `candidate_band_bass_unknown` | `not_asked` | Candidate band_bass is null, so the required frequency evidence is incomplete |
| `candidate_band_bass_unreliable` | `not_asked` | Candidate band_bass declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_band_energy_zero` | `not_asked` | All six candidate band ratios are 0, so the candidate side of the frequency evidence carries no energy to compare |
| `candidate_band_high_mid_unknown` | `not_asked` | Candidate band_high_mid is null, so the required frequency evidence is incomplete |
| `candidate_band_high_mid_unreliable` | `not_asked` | Candidate band_high_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_band_high_unknown` | `not_asked` | Candidate band_high is null, so the required frequency evidence is incomplete |
| `candidate_band_high_unreliable` | `not_asked` | Candidate band_high declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_band_low_mid_unknown` | `not_asked` | Candidate band_low_mid is null, so the required frequency evidence is incomplete |
| `candidate_band_low_mid_unreliable` | `not_asked` | Candidate band_low_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_band_mid_unknown` | `not_asked` | Candidate band_mid is null, so the required frequency evidence is incomplete |
| `candidate_band_mid_unreliable` | `not_asked` | Candidate band_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_band_sub_unknown` | `not_asked` | Candidate band_sub is null, so the required frequency evidence is incomplete |
| `candidate_band_sub_unreliable` | `not_asked` | Candidate band_sub declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_f0_unreliable` | `not_asked` | The candidate fundamental is known but declares a confidence below 0.80, so the tonal reliability guard blocks the question |
| `candidate_key_unknown` | `not_asked` | Candidate key is null, so the required tonal evidence is incomplete |
| `candidate_key_unreliable` | `not_asked` | Candidate key declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_spectral_centroid_unknown` | `not_asked` | Candidate spectral centroid is null, so the required texture evidence is incomplete |
| `candidate_spectral_centroid_unreliable` | `not_asked` | Candidate spectral centroid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_spectral_rolloff_unknown` | `not_asked` | Candidate spectral roll-off is null, so the required texture evidence is incomplete |
| `candidate_spectral_rolloff_unreliable` | `not_asked` | Candidate spectral roll-off declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_tempo_unknown` | `not_asked` | Candidate tempo is null, so the required rhythmic evidence is incomplete |
| `candidate_tempo_unreliable` | `not_asked` | Candidate tempo declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_transient_position_unknown` | `not_asked` | Candidate transient position is null, so the required transient evidence is incomplete |
| `candidate_transient_position_unreliable` | `not_asked` | Candidate transient position declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_attack_unknown` | `not_asked` | Kick attack is null, so the required transient evidence is incomplete |
| `kick_attack_unreliable` | `not_asked` | Kick attack declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_bass_unknown` | `not_asked` | Kick band_bass is null, so the required frequency evidence is incomplete |
| `kick_band_bass_unreliable` | `not_asked` | Kick band_bass declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_energy_zero` | `not_asked` | All six kick band ratios are 0, so the kick side of the frequency evidence carries no energy to compare |
| `kick_band_high_mid_unknown` | `not_asked` | Kick band_high_mid is null, so the required frequency evidence is incomplete |
| `kick_band_high_mid_unreliable` | `not_asked` | Kick band_high_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_high_unknown` | `not_asked` | Kick band_high is null, so the required frequency evidence is incomplete |
| `kick_band_high_unreliable` | `not_asked` | Kick band_high declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_low_mid_unknown` | `not_asked` | Kick band_low_mid is null, so the required frequency evidence is incomplete |
| `kick_band_low_mid_unreliable` | `not_asked` | Kick band_low_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_mid_unknown` | `not_asked` | Kick band_mid is null, so the required frequency evidence is incomplete |
| `kick_band_mid_unreliable` | `not_asked` | Kick band_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_sub_unknown` | `not_asked` | Kick band_sub is null, so the required frequency evidence is incomplete |
| `kick_band_sub_unreliable` | `not_asked` | Kick band_sub declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_decay_unknown` | `not_asked` | Kick decay is null, so the required transient evidence is incomplete |
| `kick_decay_unreliable` | `not_asked` | Kick decay declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_f0_unreliable` | `not_asked` | The kick fundamental is known but declares a confidence below 0.80, so the tonal reliability guard blocks the question |
| `kick_key_unknown` | `not_asked` | Kick key is null, so the required tonal evidence is incomplete |
| `kick_key_unreliable` | `not_asked` | Kick key declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_spectral_centroid_unknown` | `not_asked` | Kick spectral centroid is null, so the required texture evidence is incomplete |
| `kick_spectral_centroid_unreliable` | `not_asked` | Kick spectral centroid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_spectral_rolloff_unknown` | `not_asked` | Kick spectral roll-off is null, so the required texture evidence is incomplete |
| `kick_spectral_rolloff_unreliable` | `not_asked` | Kick spectral roll-off declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_transient_strength_unknown` | `not_asked` | Kick transient strength is null, so the required transient evidence is incomplete |
| `kick_transient_strength_unreliable` | `not_asked` | Kick transient strength declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `song_context_absent` | `not_asked` | No song context was supplied at all, so a dimension that needs the song tempo is not asked |
| `song_tempo_unknown` | `not_asked` | Song tempo is null, so the required rhythmic evidence is incomplete |
| `song_tempo_unreliable` | `not_asked` | Song tempo declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |

These are #13's `QUESTION_UNAVAILABLE_CODES`, meanings copied from
`_docs/jev-questions.md`; the adapter only carries the code through verbatim, never
re-interprets it and never sends one of these questions.

## Response error codes

| Code | State | Meaning |
| --- | --- | --- |
| `dimension_mismatch` | `invalid_result` | The response echoes a different contract dimension than the asked one |
| `invalid_abstention` | `invalid_result` | A null `label` does not carry exactly the `model_abstained` reason with no confidence and no probabilities, or a labeled response carries a reason |
| `invalid_confidence` | `invalid_result` | confidence` is not a finite number in [0, 1] |
| `invalid_probabilities` | `invalid_result` | probabilities` is not a five-entry list naming each contract label exactly once, or an entry is not an object with exactly `label` and `probability`, or a probability is not a number (booleans included) |
| `invalid_question` | `invalid_result` | The value passed as the asked question is not a `JevQuestion |
| `invalid_response` | `invalid_result` | The response is not JSON text or a mapping, is not valid JSON, is not a JSON object, repeats a field name, or carries a non-finite JSON number |
| `invalid_version` | `invalid_result` | model_version` is blank or is not text |
| `label_probability_mismatch` | `invalid_result` | label` is not one of the labels attaining the highest probability; a tie is accepted |
| `missing_field` | `invalid_result` | The response object is missing one of the documented eight fields |
| `probability_out_of_range` | `invalid_result` | A probability is not a finite number in [0, 1] |
| `probability_sum` | `invalid_result` | The five probabilities do not sum to 1 within the contract's absolute tolerance of 1e-6 |
| `prompt_version_mismatch` | `invalid_result` | The response does not echo the asked `prompt_version |
| `question_id_mismatch` | `invalid_result` | The response does not echo the asked `question_id |
| `question_unavailable` | `invalid_result` | A response was offered for an `UnavailableQuestion`, so a question this product refused can never receive an accepted judgment |
| `unexpected_field` | `invalid_result` | The response object contains a field outside the documented eight; an overall `compatibility`, `score`, `rank` or `overall` field is rejected here |
| `unsupported_dimension` | `invalid_result` | The response `dimension` is outside the six contract literals, for example the plan-era alias `frequencyFit |
| `unsupported_label` | `invalid_result` | The response `label` is not one of the five contract `Label` literals, for example `GOOD`, `very_poor` or `rating-good |

These are #13's `RESPONSE_ERROR_CODES`, meanings copied from
`_docs/jev-questions.md`. An `invalid_result` outcome records the code
verbatim, is terminal and is never retried. `invalid_question` and
`question_unavailable` cannot travel through this adapter: it never hands #13 a bad
question and never sends an `UnavailableQuestion`.

## Privacy and what the captured requests prove

The adapter sends exactly `question.to_json()` for every question it asks and adds no
field, header or metadata of its own beyond the transport's own authentication. The caller's
`request_id` is never sent. The payload carries `question_id`,
`dimension`, `prompt_version`, `instruction`,
`evidence` and `withheld` and nothing else: no sample ID, no local path, no file
name, no frame count, no audio and no audio bytes.

`tests/fixtures/jev/captured-requests.json` is an array of
`{name, request_id, question_case, payload_json}` records capturing what the adapter
actually sent for the #13 privacy case `frequency_payload_carries_no_sample_identity` —
whose synthetic Windows path declares the distinctive tokens `zzz-distinctive-9f8a`,
`silent-monolith-9f8a.wav` and `zeta-distinctive-9f8a-id` with a frame count of
13579 — plus three ordinary cases (`full_frequency`, `full_transient`,
`full_tonal`). `payload_json` is the exact JSON text the transport received. A
test rebuilds each referenced
`tests/fixtures/jev/question-cases.json` case, runs it through a recording
transport and asserts the captured payload equals both `question.to_json()` and the
fixture, and that none of those tokens, no sample ID, no audio bytes and no `local_path`
or `frame_count` text appears in the capture, the call log, any error message or
`run.to_json()`.

Reviewer check: open `tests/fixtures/jev/captured-requests.json` and confirm each
`payload_json` carries only `question_id`, `dimension`,
`prompt_version`, `instruction`, `evidence` and
`withheld`.

## Identity and ordering

`JevScoringRequest(request_id, question)` pairs the caller's own non-blank
`request_id` with a #13 `JevQuestion` or `UnavailableQuestion`.
`JevOutcome(request_id, question_id, dimension, state, code, attempts, judgment, elapsed_ms)`
carries both identities separately and `question_id=None` when this product refused to ask
the question, because an `UnavailableQuestion` has no digest. The adapter returns one
outcome per request in input order, never a mapping keyed by `question_id`, and
`len(run.outcomes) == len(requests)` whatever happens.

Because #13's `question_id` is a digest of the facts alone, one batch may legitimately
contain two requests that share it — the same facts for two candidates, or the same question twice.
Each such request gets its own outcome, its own send and its own `attempts`: no
de-duplication and no caching happens ([#26](https://github.com/gmphto/tera/issues/26)). Two
requests that share one `request_id`, a blank or missing `request_id`, an entry
that is not a `JevScoringRequest` or a question that is not a #13 question type raise
`JevAdapterError` with `duplicate_request_id` or `invalid_requests`
before any send, and a sequence longer than `max_batch_size` raises
`batch_too_large` before any send.

## What the run records for #15 and #19

`JevScoringRun(adapter_version, source, interface_name, prompt_version, model_versions,
cancelled, outcomes, elapsed_ms)` records `adapter_version = "jev-adapter-v1"`; a
`source` in `TRANSPORT_SOURCES` or `"unavailable"`; the transport's own
`interface_name` or `None`; #13's `PROMPT_VERSION` imported rather than
re-declared; `model_versions` as the sorted unique non-blank model versions of every
accepted judgment and abstention in the batch, empty when nothing was accepted; whether the run was
cancelled; the ordered outcomes; and a non-negative adapter-observed `elapsed_ms` in whole
milliseconds. Both records expose `to_dict()`, `to_json()` with sorted keys and
`from_dict()`/`from_json()`, round-trip exactly, and carry no credential,
endpoint, local path, filename, sample ID or audio.

A later task consumes the run without asking the service anything for that batch. Only
`source="interface"` outcomes may be counted as live Jev evidence: a
`source="double"` outcome is contract evidence and must never be reported as live Jev lift.
The double exists to prove the adapter, not to produce product evidence, and it can never support
a lift claim ([#19](https://github.com/gmphto/tera/issues/19),
[#20](https://github.com/gmphto/tera/issues/20)).

## Fixture shapes

`tests/fixtures/jev/adapter-cases.json` is an array of named batch scenarios. Each case
carries:

| Field | Meaning |
| --- | --- |
| `name` | The case name used as the test id |
| `batch` | Entries of `{request_id, question_case}` naming a case in `tests/fixtures/jev/question-cases.json`, or `{request_id, unavailable: "<code>"}` naming a `QUESTION_UNAVAILABLE_CODES` code and building the #13 case that declares it |
| `config` | Optional `JevAdapterConfig` overrides, applied before the call |
| `script` | Optional script keyed by `request_id`; the harness resolves it to the built `question_id` and concatenates the entries of requests that share one digest |
| `cancel_after` | Optional number of completed sends after which the `cancelled` callable returns truthy |
| `clock` | Optional non-negative monotonic readings in seconds, consumed in call order and repeating the final value |
| `credentials` | `"double"` (default) runs the case through the contract double; `"absent"` runs it with no transport and `environ={}` |
| `expects` | `source`, `interface_name`, the ordered `state`/`code`/`attempts` triples, `sends`, and optionally `model_versions`, `cancelled` and `elapsed_ms`; an error case declares `error` with a code in `ADAPTER_ERROR_CODES` instead |

The cases cover an all-judged batch, a judged plus a malformed batch, a service error that retries
into a judgment, a timeout that exhausts its retries, cancellation after the first response,
cancellation between two attempts, the deadline before a later request, an empty batch, the same
`question_id` twice, the absent-credentials batch, an over-limit batch, an invalid
configuration, a duplicate and a blank `request_id`, and a batch built from #13's
unavailable cases. Every code in `TRANSPORT_ERROR_CODES`,
`RETRYABLE_TRANSPORT_CODES`, `NOT_ATTEMPTED_CODES`,
`UNAVAILABLE_CODES` and `ADAPTER_ERROR_CODES` is exercised by at least one
case, and every value of `OUTCOME_STATES` appears in at least one case.

A script entry carries exactly one of:

| Entry | Meaning |
| --- | --- |
| `{"judgment": {"probabilities": [five numbers], "confidence": n, "model_version": "m"}}` | A labeled judgment; the label is the first contract label attaining the highest probability |
| `{"abstain": {"model_version": "m"}}` | The model's own `model_abstained` abstention |
| `{"text": "<response text>"}` | Returned verbatim, so #13 rejects it |
| `{"error": "<code>"}` | Raises `JevTransportError`; rejected at construction unless the code is in `TRANSPORT_ERROR_CODES` |

A single entry is accepted wherever a one-entry sequence is meant, a `question_id` with
no script entry uses the double's `default`, and any occurrence beyond the scripted ones
repeats the last scripted entry.

`tests/fixtures/jev/captured-requests.json` is described under privacy above. Both
fixtures are loaded by the tests rather than restated in them.

The adapter reuses #13's read-only inputs and never modifies one:
`tests/fixtures/jev/question-cases.json`, `tests/fixtures/jev/response-cases.json`
and `tests/fixtures/jev/malformed-responses.json`, described in
`_docs/jev-questions.md`.

## Local verification

```text
uv run pytest tests/test_jev_adapter.py tests/test_jev_double.py --basetemp .pytest_cache/jev-adapter
uv run pytest tests/test_jev_integration.py --basetemp .pytest_cache/jev-integration
uv run pytest --basetemp .pytest_cache/jev-full
```

The first command proves everything that does not need a service: the bounds and the pre-send
rejection, the absent-credentials run, the redaction rule, the payload capture and privacy
assertions, the not-asked and invalid-result loops over #13's fixtures, the retry and backoff
schedule, cancellation, the deadline bound, the record round-trips, the double's determinism and the
injected-opener HTTP checks. It opens no socket, needs no credential and waits on no wall clock. The
second command runs only with credentials; without them its one test skips and proves nothing. The
third command runs the whole suite, where the four pre-existing sandbox-only subprocess failures in
`tests/test_batch.py`, `tests/test_evaluation_manifest.py` and
`tests/test_evaluation_prepare.py` remain the only failures.

## Unverified without credentials

With no `TERA_JEV_ENDPOINT` and no `TERA_JEV_API_KEY`, every criterion above but
the live check is provable locally. Three things no local evidence can establish, and which stay
unverified until a credentialed run observes them:

* the real TypeSafe Jev wire mapping, recorded here as assumed;
* the real `model_version` values the service returns;
* the real service's timing and failure behaviour.

The one live check is reported, not assumed:

```text
real TypeSafe Jev integration: UNVERIFIED - TERA_JEV_ENDPOINT/TERA_JEV_API_KEY are not set
```

A skipped integration check is never recorded as a passing live check, and no other criterion
depends on credentials.

## Out of scope

* Weighted combination, confidence thresholds, alternatives and the DSP-only fallback:
  [#15](https://github.com/gmphto/tera/issues/15). This adapter returns recorded outcomes and
  applies no weight.
* Decision caching, question de-duplication and invalidation:
  [#26](https://github.com/gmphto/tera/issues/26).
* Surfacing questions, abstentions, judgments or adapter failures through the local API or client:
  [#28](https://github.com/gmphto/tera/issues/28).
* Storing runs, judgments or model versions in SQLite: [#21](https://github.com/gmphto/tera/issues/21).
* Representing an unscored or failed candidate in the recommendation contracts:
  [#62](https://github.com/gmphto/tera/issues/62). `backend/contracts.py` stays unchanged
  at schema 1.0.
* Ranking comparison, dataset recording and the Jev-only versus hybrid lift question:
  [#19](https://github.com/gmphto/tera/issues/19), [#20](https://github.com/gmphto/tera/issues/20).
* Measuring latency thresholds and tuning throughput: [#37](https://github.com/gmphto/tera/issues/37).
* Bounded parallel dispatch for a whole candidate shortlist:
  [#64](https://github.com/gmphto/tera/issues/64). This adapter stays sequential.
* Choosing which dimensions to ask for a candidate and reconciling judgments with measured facts:
  [#15](https://github.com/gmphto/tera/issues/15). The adapter scores the questions it is given and
  never builds one.
* A producer-specified intended arrangement role: [#63](https://github.com/gmphto/tera/issues/63).
* Any producer, library or audio data: the adapter never opens a file and every fixture is
  synthetic.
