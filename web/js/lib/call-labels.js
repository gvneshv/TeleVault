/**
 * Call labels: showing the archive's call messages in the interface language.
 *
 * Telegram delivers calls as service messages, and the archiver stores them as ordinary rows with a synthesized ENGLISH label
 * (handlers/helpers.py format_call_text), e.g. "[Voice call · 1 min 05 sec]".
 * Stored text is archive content and is never rewritten, so the translation happens here, at display time, for exactly the labels that function can produce -
 * nothing else is touched.
 *
 * The pattern below must stay in step with format_call_text(); tests/unit/test_call_labels_js.py checks the two against each other.
 * A text that does not match (a normal message that merely looks similar, or a label from a future version) is returned unchanged.
 */

const FIXED = {
  "[Missed call]": "call.missed",
  "[Declined call]": "call.declined",
  "[Call ended (disconnected)]": "call.endedDisconnected",
};

// [<kind> · <duration or outcome>( (disconnected))?]   kind: Voice call | Video call | Call
//  duration: "N sec" or "N min SS sec"
const CALL =
  /^\[(Voice call|Video call|Call) · (?:(unknown outcome)|(\d+) sec|(\d+) min (\d{2}) sec)( \(disconnected\))?\]$/;

/**
 * @param {string} text - a stored message text
 * @param {(key: string) => string} t - the translator (i18n.js)
 * @returns {string} the label in the interface language, or `text` itself when it is not a call label
 */
export function localizeCallLabel(text, t) {
  if (typeof text !== "string" || !text.startsWith("[")) return text;
  const fixed = FIXED[text];
  if (fixed) return t(fixed);
  const m = CALL.exec(text);
  if (!m) return text;
  const [, kind, unknown, sec, min, minSec, disconnected] = m;
  const name = t(
    kind === "Voice call"
      ? "call.voice"
      : kind === "Video call"
        ? "call.video"
        : "call.generic",
  );
  let detail;
  if (unknown) detail = t("call.unknown");
  else if (sec !== undefined) detail = t("call.sec").replace("{n}", sec);
  else detail = t("call.minSec").replace("{m}", min).replace("{s}", minSec);
  const tail = disconnected ? ` (${t("call.disconnected")})` : "";
  return `[${name} · ${detail}${tail}]`;
}
