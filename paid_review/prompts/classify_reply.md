# Classify Junior's Free-Text Reply

A junior is replying to a specific finding in a review session. Their
reply is free-form text. Classify it into ONE of the four review
statuses so the cursor can advance.

## The finding being answered

Pillar: {pillar}
Severity: {severity}
Issue: {issue}
Suggested fix: {suggest}

## Junior's reply

{reply}

## Statuses

- **accepted** — junior agrees with the finding and will revise the
  document. Cues: "OK I'll fix", "good catch", "you're right", "改"
- **rejected** — junior disagrees with the finding (with reason). Cues:
  "actually X is fine because", "we already considered Y", contains
  reasoning that defends the original. The reply MUST contain a
  substantive rebuttal — bare "no" without reason is `unresolvable`.
- **modified** — junior offers an alternative fix, different from the
  suggested one. Cues: "instead of X, I'll do Y", "how about Z",
  contains a concrete counter-proposal.
- **unresolvable** — junior says they can't address it / out of scope /
  needs owner input. Cues: "don't know", "can't get that data",
  "would need Jimmy to decide", or bare "no" without reason.

## Output (strict JSON, no prose)

Return ONLY:

```json
{
  "status": "accepted" | "rejected" | "modified" | "unresolvable",
  "confidence": 0.0-1.0,
  "rationale": "≤20-word explanation of why this status"
}
```

If you genuinely can't tell, return status="modified" + low confidence
(this lets the session advance instead of stalling).

Output JSON now:
