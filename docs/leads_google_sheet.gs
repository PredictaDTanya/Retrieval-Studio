/**
 * Retrieval Studio — lead capture endpoint (Google Apps Script).
 *
 * Receives the JSON that the app POSTs to RETRIEVAL_LEADS_WEBHOOK for each
 * gated-mode signup and appends a row to a Google Sheet. This IS your durable
 * lead database (survives Streamlit Community Cloud restarts, unlike a local
 * file).
 *
 * Setup:
 *   1. Create a Google Sheet (sheets.new). Note its name.
 *   2. Extensions -> Apps Script. Delete the sample, paste this file.
 *   3. (Recommended) set TOKEN below to a secret string.
 *   4. Deploy -> New deployment -> type "Web app":
 *        Execute as: Me
 *        Who has access: Anyone
 *      Deploy, authorise, and COPY the /exec Web app URL.
 *   5. In the app's environment / Streamlit Secrets:
 *        RETRIEVAL_LEADS_WEBHOOK = "<the /exec URL>"
 *        RETRIEVAL_LEADS_TOKEN   = "<same secret as TOKEN>"   (if you set one)
 *
 * Test: Deploy, then in a terminal:
 *   curl -X POST -H "Content-Type: application/json" \
 *     -d '{"name":"Test","email":"t@example.com","org":"","timestamp":"now","session_id":"x","source":"test","_token":"<TOKEN>"}' \
 *     "<the /exec URL>"
 * A row should appear on the "Leads" tab.
 */

var SHEET_NAME = 'Leads';
var TOKEN = '';   // must equal RETRIEVAL_LEADS_TOKEN; leave '' to disable the check
var HEADERS = ['Timestamp', 'Name', 'Email', 'Organisation',
               'Session', 'Source'];

function doPost(e) {
  try {
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (TOKEN && String(body._token || '') !== TOKEN) {
      return _out('forbidden');
    }
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sh = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
    if (sh.getLastRow() === 0) {
      sh.appendRow(HEADERS);
    }
    sh.appendRow([
      body.timestamp || new Date().toISOString(),
      body.name || '',
      body.email || '',
      body.org || '',
      body.session_id || '',
      body.source || ''
    ]);
    return _out('ok');
  } catch (err) {
    return _out('error: ' + err);
  }
}

function doGet() {           // health check
  return _out('fan-out lead endpoint: live');
}

function _out(text) {
  return ContentService.createTextOutput(text)
      .setMimeType(ContentService.MimeType.TEXT);
}
