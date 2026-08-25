const REVIEW_ID_RE = /^weekly-\d{4}-\d{2}-\d{2}-\d{4}-\d{2}-\d{2}$/;
const ITEM_ID_RE = /^[a-f0-9]{16}$/;
const ALLOWED_LABELS = new Set(["kept", "action_needed", "digest_and_trash"]);

function doGet(e) {
  const reviewId = String((e && e.parameter && e.parameter.review_id) || "");
  if (!REVIEW_ID_RE.test(reviewId)) {
    return renderStatus_("", "Άνοιξε το review από το συνημμένο του εβδομαδιαίου email.", 0);
  }
  return renderStatus_(reviewId, "Έλεγχος κατάστασης…", 0);
}

function doPost(e) {
  try {
    assertAllowedUser_();
    const params = (e && e.parameter) || {};
    const reviewId = String(params.review_id || "");
    const itemIdsText = String(params.item_ids || "");
    const suppliedToken = String(params.approval_token || "");
    const itemIds = itemIdsText.split(",").filter(Boolean);

    if (!REVIEW_ID_RE.test(reviewId)) throw new Error("Μη έγκυρο Review ID.");
    if (!itemIds.length || new Set(itemIds).size !== itemIds.length ||
        itemIds.some(id => !ITEM_ID_RE.test(id))) {
      throw new Error("Μη έγκυρη λίστα review items.");
    }
    const expectedToken = approvalToken_(reviewId, itemIdsText);
    if (!constantTimeEqual_(suppliedToken, expectedToken)) {
      throw new Error("Το review δεν έχει έγκυρη υπογραφή.");
    }

    const selections = {};
    itemIds.forEach(id => {
      const value = String(params["choice_" + id] || "");
      if (!ALLOWED_LABELS.has(value)) {
        throw new Error("Υπάρχει μη έγκυρη επιλογή label.");
      }
      selections[id] = value;
    });
    const submittedChoiceKeys = Object.keys(params).filter(key => key.indexOf("choice_") === 0);
    if (submittedChoiceKeys.length !== itemIds.length) {
      throw new Error("Το review δεν περιλαμβάνει απόφαση για κάθε email.");
    }

    const existing = getApplyStatus(reviewId);
    if (existing.status === "complete") {
      return renderStatus_(reviewId, "Το review είχε ήδη εφαρμοστεί επιτυχώς.", 0);
    }
    const dispatchedAtMs = Date.now();
    dispatchApply_(reviewId, selections);
    return renderStatus_(reviewId, "Η επιβεβαίωση καταχωρίστηκε. Οι αλλαγές εφαρμόζονται…", dispatchedAtMs);
  } catch (error) {
    return renderStatus_("", "Η επιβεβαίωση δεν έγινε: " + safeMessage_(error), 0);
  }
}

function getApplyStatus(reviewId, newerThanMs) {
  if (!REVIEW_ID_RE.test(String(reviewId || ""))) {
    return {status: "error", message: "Μη έγκυρο Review ID."};
  }
  const props = PropertiesService.getScriptProperties();
  const repository = requiredProperty_(props, "GITHUB_REPOSITORY");
  const stateBranch = props.getProperty("STATE_BRANCH") || "gmail-fomo-state";
  const path = ".gmail-fomo/weekly-review-ledgers/" + reviewId + ".json";
  const response = githubFetch_(
    "https://api.github.com/repos/" + repository + "/contents/" + path +
      "?ref=" + encodeURIComponent(stateBranch),
    {method: "get", muteHttpExceptions: true}
  );
  if (response.getResponseCode() === 404) return {status: "pending"};
  if (response.getResponseCode() !== 200) {
    return {status: "error", message: "Δεν ήταν δυνατός ο έλεγχος της εφαρμογής."};
  }
  try {
    const envelope = JSON.parse(response.getContentText());
    const content = Utilities.newBlob(Utilities.base64Decode(envelope.content)).getDataAsString();
    const ledger = JSON.parse(content);
    if (ledger.review_id !== reviewId) throw new Error("ledger mismatch");
    const appliedAtMs = Date.parse(String(ledger.applied_at || ""));
    if (!Number.isFinite(appliedAtMs)) throw new Error("invalid ledger timestamp");
    if (ledger.status === "incomplete" && Number(newerThanMs || 0) > appliedAtMs) {
      return {status: "pending"};
    }
    return {
      status: ledger.status,
      counts: ledger.counts || {},
      message: ledger.status === "incomplete" ? "Η εφαρμογή σταμάτησε με ασφάλεια. Έλεγξε το workflow." : ""
    };
  } catch (error) {
    return {status: "error", message: "Η απόδειξη εφαρμογής δεν ήταν έγκυρη."};
  }
}

function dispatchApply_(reviewId, selections) {
  const props = PropertiesService.getScriptProperties();
  const repository = requiredProperty_(props, "GITHUB_REPOSITORY");
  const ref = props.getProperty("GITHUB_REF") || "main";
  const url = "https://api.github.com/repos/" + repository +
    "/actions/workflows/apply-weekly-review.yml/dispatches";
  const response = githubFetch_(url, {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({
      ref: ref,
      inputs: {
        review_id: reviewId,
        selections_json: JSON.stringify(selections)
      }
    }),
    muteHttpExceptions: true
  });
  if (![200, 204].includes(response.getResponseCode())) {
    throw new Error("Το GitHub workflow δεν ξεκίνησε (HTTP " + response.getResponseCode() + ").");
  }
}

function githubFetch_(url, options) {
  const props = PropertiesService.getScriptProperties();
  const token = requiredProperty_(props, "GITHUB_TOKEN");
  const request = Object.assign({}, options || {});
  request.headers = Object.assign({}, request.headers || {}, {
    Authorization: "Bearer " + token,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
  });
  return UrlFetchApp.fetch(url, request);
}

function approvalToken_(reviewId, itemIdsText) {
  const secret = requiredProperty_(PropertiesService.getScriptProperties(), "APPROVAL_SECRET");
  const bytes = Utilities.computeHmacSha256Signature(
    reviewId + "\n" + itemIdsText,
    secret,
    Utilities.Charset.UTF_8
  );
  return bytes.map(value => (value & 255).toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual_(left, right) {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function assertAllowedUser_() {
  const allowed = PropertiesService.getScriptProperties().getProperty("ALLOWED_USER_EMAIL");
  if (!allowed) return;
  const active = Session.getActiveUser().getEmail();
  if (active && active.toLowerCase() !== allowed.toLowerCase()) {
    throw new Error("Δεν επιτρέπεται η πρόσβαση από αυτόν τον λογαριασμό.");
  }
}

function requiredProperty_(props, name) {
  const value = props.getProperty(name);
  if (!value) throw new Error("Λείπει η ασφαλής ρύθμιση " + name + ".");
  return value;
}

function renderStatus_(reviewId, initialMessage, newerThanMs) {
  const template = HtmlService.createTemplateFromFile("Status");
  template.reviewId = reviewId;
  template.initialMessage = initialMessage;
  template.newerThanMs = Number(newerThanMs || 0);
  return template.evaluate().setTitle("Weekly Gmail review");
}

function safeMessage_(error) {
  return error && error.message ? String(error.message) : "Άγνωστο σφάλμα.";
}
