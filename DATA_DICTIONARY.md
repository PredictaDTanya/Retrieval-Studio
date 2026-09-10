# Data dictionary — every export field

All tables/sheets carry `dataset_id`, `study_id` and (where applicable)
`observation_id`, `prompt_id`, model, timestamp and location.

## observations / "Runs" sheet
| Field | Meaning |
|---|---|
| observation_id | Unique id for one prompt × run execution |
| dataset_id / study_id | The dataset (isolation unit) and study this row belongs to |
| prompt_id / run_number | Which prompt, which repetition |
| requested_model | Model the user asked for |
| actual_model | Model the API reported in the response (`response.model`). Displayed everywhere; never silently substituted |
| response_id | The raw API response id (`resp_...`) — provenance key back to raw JSON |
| response_status | API response status (`completed`, `incomplete`, `failed`, ...) |
| incomplete_reason | `incomplete_details.reason` when the API cut the response short; such rows are classified `incomplete`, never `ok` |
| response_created_at / service_tier | Response provenance as returned by the API |
| api_error_code / api_error_message | The ACTUAL error from a failed response body (such rows are classified `error`) |
| timestamp | UTC call time |
| location | Approximate user location sent with web_search |
| search_mode | Tool + tool_choice (`web_search:auto`) |
| prompt_set_version / schema_version | Prompt-library label; row schema version |
| status / error | `ok` or `error` + the exact error text |
| prompt_text / answer_text | Input prompt; complete final answer |
| reasoning_summary | Model-generated reasoning summary if returned, else NULL. Never fabricated; not the hidden chain of thought |
| input/output/total/reasoning/cached_tokens | Usage from the API response |
| cost_usd | Computed from the pricing table (tokens + per-search fee), cached tier honoured |
| latency_ms | Wall-clock call latency |
| num_search_actions | web_search_call actions of type `search` |
| num_queries | Fan-out queries WITH text |
| num_queries_missing_text | Search actions that returned no query text |
| num_site_queries | Queries containing a `site:` operator |
| num_open_page / num_find_in_page | open_page / find_in_page actions |
| num_search_sources | Source URLs returned by search actions |
| num_citations | Total url_citation annotations (repeats counted) |
| num_unique_cited_urls | Distinct canonical cited URLs |

## fanout_queries
| Field | Meaning |
|---|---|
| seq / batch_index | Position in output order; which search batch |
| query | Verbatim query text, NULL when the API returned none |
| query_missing | 1 = search action without query text (never inferred) |
| has_site_operator / site_domain | `site:` present; the domain after it |
| entity_hits | JSON list of tracked entity_ids the query targets |
| taxonomy | One of the 15 stable cluster categories |

## search_sources / opened_pages / inpage_searches
| Field | Meaning |
|---|---|
| seq | Output-order position |
| action_seq | (sources) which search action returned it |
| raw_url | Exactly as returned (tracking params preserved) |
| canonical_url | https, www/tracking-params/fragment stripped — aggregation key |
| domain | Registrable host of the canonical URL |
| title | Returned page title (sources only) |
| source_type | Source kind as returned — `url`, or real-time feeds like `oai-weather` / `oai-sports` / `oai-finance` (preserved, not reduced to URL) |
| pattern | (inpage_searches) the find_in_page search pattern |

## citations (aggregated per canonical URL)
| Field | Meaning |
|---|---|
| raw_url / canonical_url / domain / title | As above |
| citation_count | Times this canonical URL was cited in the answer (repeats preserved) |
| first_annotation_index | Character index of the first citation annotation |

## citation_occurrences (one row per citation annotation — claim mapping)
| Field | Meaning |
|---|---|
| occurrence_number | 1..n within the observation, in answer order |
| raw_url / canonical_url / domain / title | The cited source |
| start_index / end_index | Character span in the joined answer text |
| cited_text | The exact answer span the annotation covers |
| answer_sentence | The sentence containing the citation — "X was cited to support this claim" |

## search_calls (per web_search_call provenance)
| Field | Meaning |
|---|---|
| sequence | Output-order position of the call |
| search_call_id / search_call_status | The API's id and status for the call — surfaces failed/incomplete search actions inside an otherwise completed response |
| action_type | search / open_page / find_in_page |
| query_count / source_count | Queries carried and sources returned by this call |

## source_entity (bridge table)
| Field | Meaning |
|---|---|
| canonical_url / domain | The source |
| entity_id | Tracked entity it relates to |
| relation | `first_party` (entity's own domain) or `mentions` (entity named in title/URL) |
| evidence_stage | Stage the link was observed at: `search_source` / `opened_page` / `inpage_search` / `citation` — one row per stage |
| evidence | Title/URL snippet supporting the link |

## entity_runs
Per observation × entity booleans, one per STAGE of the retrieval process:
`search_targeted` (a query targeted the entity), `source_returned` (its
domain came back in search results), `page_opened` (its page was opened),
`page_searched` (find_in_page ran on its page), `cited` (its domain in the
final answer's citations), `named_in_answer`, `mentioned_anywhere`.
These feed the entity rates (rate = share of runs where the boolean is 1).

## Entity Visibility sheet
mention_rate / search_targeting_rate / source_returned_rate /
page_opened_rate / page_searched_rate / citation_rate /
answer_mention_rate (named in the final answer — called that, not
"recommendation rate", because recommendation detection is not implemented)
+ prompts_appeared + supporting_sources.

## Prompt Coverage sheet
Per prompt: runs, search_trigger_rate, avg_fanouts, site_rate, primary-entity
mention/citation/answer-mention rates, top_competing_entity,
dominant_source_domains, identified gaps.

## Citation Gaps sheet
prompt, primary_cited_runs/runs, gap_type (one of 12 classified types),
evidence, supporting_queries, winning_domains — every diagnosis links to the
specific queries/sources behind it.

## Query Clusters sheet
cluster (stable taxonomy), queries, site_probes, entity_targeted,
runs_touched, co_cited_domains, example_queries.

## Domain leaderboard (Sources page)
ONE row per canonical domain: class (first_party / directory /
review_platform / government / social_platform / publication / awards_site /
platform_docs / other_third_party), returned_in_search, pages_opened,
citations, unique_cited_urls, runs_citing, entity_links (attribution
summary — a separate dimension from class).
