/** Normalize common browser-STT mishears before send / unlock. */

const CONFIRM_UTTERANCE =
  /^\s*(yes|yeah|yep|yup|y|ok|okay|sure|confirm|confirmed|firm|can\s*fun|can\s*firm|come\s*firm|con\s*firm|confurm|confrom|conform|confem|looks\s+good|go\s+ahead|proceed|sounds\s+good|please\s+do|do\s+it)\s*[.!]?\s*$/i;

const CONFIRM_TOKEN =
  /\b(yes|yeah|yep|yup|confirm|confirmed|sure|ok(?:ay)?|firm|go\s+ahead|proceed|looks\s+good|sounds\s+good|can\s*fun|can\s*firm|come\s*firm|con\s*firm|confurm|confrom|conform|confem)\b/i;

const PACKED_UTTERANCE =
  /^\s*(packed|packt|packet|packe|packd|pact|pac|pat|fact|pack|busy|intense|full[\s-]?day)\s*(?:pace)?\s*[.!]?\s*$/i;

const PACKED_TOKEN =
  /\b(packed|packt|packet|packe|packd|pact|pac|pat|fact|pack(?:ed)?)\b/gi;

export function normalizeSttMessage(message: string): string {
  let text = (message || "").trim().replace(/\s+/g, " ");
  if (!text) return text;

  if (CONFIRM_UTTERANCE.test(text)) {
    return "confirm";
  }

  if (PACKED_UTTERANCE.test(text)) {
    return "packed";
  }

  text = text.replace(PACKED_TOKEN, "packed");

  if (
    text.split(/\s+/).length <= 3 &&
    CONFIRM_TOKEN.test(text) &&
    !/\b(day|add|remove|swap|change|make)\b/i.test(text)
  ) {
    return "confirm";
  }

  return text;
}
