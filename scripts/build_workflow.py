#!/usr/bin/env python3
"""Builds the n8n workflow JSON for AI invoice processing (AP automation)."""
import json, os, uuid

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ai-invoice-processing.workflow.json")

def nid():
    return str(uuid.uuid4())

# ---------------------------------------------------------------- code nodes

PARSE_JS = r'''// Turns the OpenAI response into a clean invoice object and attaches e-mail metadata.
const email = $('Gmail Trigger').item.json;
let inv;
try {
  inv = JSON.parse($json.choices[0].message.content);
} catch (e) {
  inv = { is_invoice: false, notes: 'Could not parse AI output: ' + e.message };
}
const from = (email.from && email.from.value && email.from.value[0] && email.from.value[0].address)
  || (email.from && email.from.text) || '';
return {
  json: {
    received_at: new Date().toISOString(),
    email_from: from,
    email_subject: email.subject || '',
    email_id: email.id || '',
    ...inv,
  },
};
'''

VALIDATE_JS = r'''// ===== CONFIG – adjust to the client's approval policy =====
const CONFIG = {
  autoApproveMaxTotal: 1000,   // invoices above this total always go to a human
  priceIncreaseAlertPct: 10,   // flag line items whose unit price rose more than this % vs the last approved invoice
  vendorTotalSpikeFactor: 2,   // flag totals greater than N x the vendor's historical average
  mathTolerance: 0.05,         // rounding tolerance for the sum checks
  minConfidence: 0.8,          // AI confidence below this -> human review
  reviewNewVendors: true,      // first invoice from an unknown vendor -> human review
};

const r2 = n => Math.round((Number(n) || 0) * 100) / 100;
const norm = s => String(s == null ? '' : s).toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, ' ').trim();
const vendorKey = (name, taxId) => (taxId && norm(taxId)) ? 'tax:' + norm(taxId) : 'name:' + norm(name);
const parseLines = s => { try { const v = JSON.parse(s || '[]'); return Array.isArray(v) ? v : []; } catch (e) { return []; } };

// ---- ledger (Google Sheet) -> duplicate index + trusted price history
const ledger = $('Read Invoice Ledger').all().map(i => i.json).filter(r => r && r.invoice_number);
const existing = new Set();
const history = {};
for (const row of ledger) {
  const key = vendorKey(row.vendor_name, row.vendor_tax_id);
  if (String(row.status) !== 'rejected') existing.add(key + '|' + norm(row.invoice_number));
  if (!['approved', 'auto_approved'].includes(String(row.status))) continue;
  const h = history[key] || (history[key] = { totals: [], prices: {} });
  if (Number(row.total) > 0) h.totals.push(Number(row.total));
  const date = String(row.invoice_date || '');
  for (const l of parseLines(row.line_items_json)) {
    const d = norm(l.description);
    const price = Number(l.unit_price);
    if (!d || !(price > 0)) continue;
    if (!h.prices[d] || date >= h.prices[d].date) h.prices[d] = { price, date };
  }
}

// ---- check every new invoice
const seenInBatch = new Set();
return $input.all().map((item, idx) => {
  const inv = item.json;
  const reasons = [];
  const key = vendorKey(inv.vendor_name, inv.vendor_tax_id);
  const lines = Array.isArray(inv.line_items) ? inv.line_items : [];

  // 1) arithmetic
  if (lines.length) {
    const sum = r2(lines.reduce((a, l) => a + (Number(l.amount) || 0), 0));
    if (Math.abs(sum - r2(inv.subtotal)) > CONFIG.mathTolerance)
      reasons.push(`Line items add up to ${sum}, but subtotal is ${r2(inv.subtotal)}`);
  }
  const calcTotal = r2(r2(inv.subtotal) + r2(inv.tax_total));
  if (Math.abs(calcTotal - r2(inv.total)) > CONFIG.mathTolerance)
    reasons.push(`Subtotal + tax = ${calcTotal}, but total is ${r2(inv.total)}`);

  // 2) completeness / AI confidence
  if (!inv.invoice_number) reasons.push('Invoice number missing');
  if ((Number(inv.confidence) || 0) < CONFIG.minConfidence) reasons.push(`Low AI confidence (${inv.confidence})`);

  // 3) duplicates (ledger + same batch)
  const dupKey = key + '|' + norm(inv.invoice_number);
  if (inv.invoice_number && (existing.has(dupKey) || seenInBatch.has(dupKey)))
    reasons.push('POSSIBLE DUPLICATE: this vendor + invoice number is already recorded');
  seenInBatch.add(dupKey);

  // 4) history: new vendor, total spike, unit-price increases
  const h = history[key];
  if (!h) {
    if (CONFIG.reviewNewVendors) reasons.push('First invoice from this vendor');
  } else {
    if (h.totals.length) {
      const avg = h.totals.reduce((a, b) => a + b, 0) / h.totals.length;
      if (avg > 0 && r2(inv.total) > avg * CONFIG.vendorTotalSpikeFactor)
        reasons.push(`Total ${r2(inv.total)} is over ${CONFIG.vendorTotalSpikeFactor}x this vendor's average (${r2(avg)})`);
    }
    for (const l of lines) {
      const prev = h.prices[norm(l.description)];
      const up = Number(l.unit_price);
      if (prev && up > 0) {
        const pct = (up - prev.price) / prev.price * 100;
        if (pct > CONFIG.priceIncreaseAlertPct)
          reasons.push(`Price up ${pct.toFixed(1)}%: "${l.description}" ${prev.price} -> ${up} (last invoice ${prev.date})`);
      }
    }
  }

  // 5) amount policy
  if (r2(inv.total) > CONFIG.autoApproveMaxTotal)
    reasons.push(`Total ${r2(inv.total)} ${inv.currency || ''} is above the auto-approve limit (${CONFIG.autoApproveMaxTotal})`);

  const lineText = lines.slice(0, 10)
    .map(l => `• ${l.description} — ${l.quantity} x ${l.unit_price} = ${l.amount}`).join('\n')
    + (lines.length > 10 ? `\n… and ${lines.length - 10} more lines` : '');

  const message = [
    '🧾 Invoice needs approval',
    '',
    `Vendor: ${inv.vendor_name}${inv.vendor_tax_id ? ' (' + inv.vendor_tax_id + ')' : ''}`,
    `Invoice: ${inv.invoice_number || '—'}  |  Date: ${inv.invoice_date || '—'}  |  Due: ${inv.due_date || '—'}`,
    `Total: ${r2(inv.total)} ${inv.currency || ''}  (net ${r2(inv.subtotal)} + tax ${r2(inv.tax_total)})`,
    `From: ${inv.email_from}`,
    '',
    'Why it needs review:',
    ...reasons.map(r => '⚠️ ' + r),
    '',
    lineText,
  ].join('\n').slice(0, 3900);

  return {
    json: {
      ...inv,
      line_items_json: JSON.stringify(lines),
      review_reasons: reasons.join(' | '),
      needs_review: reasons.length > 0,
      telegram_message: message,
    },
    pairedItem: { item: idx },
  };
});
'''

APPLY_DECISION_JS = r'''// Merges the approver's Telegram decision back into the invoice.
const inv = $('Validate & Check History').item.json;
const approved = !!($json.data && $json.data.approved === true);
return {
  json: {
    ...inv,
    status: approved ? 'approved' : 'rejected',
    decided_by: 'telegram',
    decided_at: new Date().toISOString(),
  },
};
'''

AUTO_APPROVE_JS = r'''// Clean invoice under the limit -> approved by rules, no human needed.
return {
  json: {
    ...$json,
    status: 'auto_approved',
    decided_by: 'rules',
    decided_at: new Date().toISOString(),
  },
};
'''

SHAPE_ROW_JS = r'''// Keeps only the ledger columns (must match the header row of the "Invoices" sheet).
const COLUMNS = ['received_at', 'email_from', 'email_subject', 'vendor_name', 'vendor_tax_id',
  'invoice_number', 'invoice_date', 'due_date', 'currency', 'subtotal', 'tax_total', 'total',
  'line_items_json', 'confidence', 'review_reasons', 'status', 'decided_by', 'decided_at'];
const row = {};
for (const c of COLUMNS) row[c] = $json[c] == null ? '' : $json[c];
return { json: row };
'''

# ---------------------------------------------------------------- OpenAI request body

OPENAI_BODY = r'''={{ JSON.stringify({
  model: 'gpt-4o-mini',
  temperature: 0,
  response_format: {
    type: 'json_schema',
    json_schema: {
      name: 'invoice',
      strict: true,
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['is_invoice', 'vendor_name', 'vendor_tax_id', 'invoice_number', 'invoice_date', 'due_date',
                   'currency', 'subtotal', 'tax_total', 'total', 'line_items', 'confidence', 'notes'],
        properties: {
          is_invoice: { type: 'boolean' },
          vendor_name: { type: 'string' },
          vendor_tax_id: { type: ['string', 'null'] },
          invoice_number: { type: 'string' },
          invoice_date: { type: 'string', description: 'YYYY-MM-DD' },
          due_date: { type: ['string', 'null'], description: 'YYYY-MM-DD' },
          currency: { type: 'string', description: 'ISO 4217 code, e.g. USD, EUR, TRY' },
          subtotal: { type: 'number' },
          tax_total: { type: 'number' },
          total: { type: 'number' },
          line_items: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              required: ['description', 'quantity', 'unit_price', 'amount'],
              properties: {
                description: { type: 'string' },
                quantity: { type: 'number' },
                unit_price: { type: 'number' },
                amount: { type: 'number' }
              }
            }
          },
          confidence: { type: 'number', description: '0-1, how sure you are the extraction is correct' },
          notes: { type: 'string' }
        }
      }
    }
  },
  messages: [
    { role: 'system', content: 'You extract structured data from supplier invoices for an accounts-payable system. The text can be in any language (English, Turkish, German, Spanish...). Convert all amounts to plain numbers with a dot as decimal separator (1.234,56 -> 1234.56). Dates as YYYY-MM-DD. subtotal = net amount before tax, tax_total = total VAT/KDV/sales tax, total = amount payable. line_items.amount is the net line total. If the document is not an invoice (quote, receipt for a newsletter, contract, marketing), set is_invoice to false and fill the other fields with empty values. Never invent numbers: if something is unreadable, lower the confidence and explain in notes.' },
    { role: 'user', content: 'E-mail subject: ' + ($('Gmail Trigger').item.json.subject || '') + '\n\nDocument text:\n' + String($json.text || '').slice(0, 15000) }
  ]
}) }}'''

# ---------------------------------------------------------------- nodes

def cond(left, op_type, operation, right=None, single=False):
    c = {"id": nid(), "leftValue": left, "operator": {"type": op_type, "operation": operation}}
    if single:
        c["operator"]["singleValue"] = True
        c["rightValue"] = ""
    else:
        c["rightValue"] = right
    return c

def conditions(conds, combinator="and"):
    return {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
        "conditions": conds,
        "combinator": combinator,
    }

nodes = []
def node(name, ntype, version, params, pos, **extra):
    n = {"parameters": params, "id": nid(), "name": name, "type": ntype,
         "typeVersion": version, "position": pos}
    n.update(extra)
    nodes.append(n)
    return n

Y = 400
node("Gmail Trigger", "n8n-nodes-base.gmailTrigger", 1.2, {
    "pollTimes": {"item": [{"mode": "everyMinute"}]},
    "simple": False,
    "filters": {"q": "has:attachment filename:pdf"},
    "options": {"downloadAttachments": True},
}, [0, Y])

node("Extract PDF Text", "n8n-nodes-base.extractFromFile", 1, {
    "operation": "pdf",
    "binaryPropertyName": "attachment_0",
    "options": {},
}, [220, Y], onError="continueRegularOutput")

node("AI: Extract Invoice Data", "n8n-nodes-base.httpRequest", 4.2, {
    "method": "POST",
    "url": "https://api.openai.com/v1/chat/completions",
    "authentication": "predefinedCredentialType",
    "nodeCredentialType": "openAiApi",
    "sendBody": True,
    "specifyBody": "json",
    "jsonBody": OPENAI_BODY,
    "options": {"timeout": 120000},
}, [440, Y], retryOnFail=True, maxTries=3, waitBetweenTries=3000)

node("Parse AI Output", "n8n-nodes-base.code", 2, {
    "mode": "runOnceForEachItem",
    "jsCode": PARSE_JS,
}, [660, Y])

node("Only Invoices", "n8n-nodes-base.filter", 2.2, {
    "conditions": conditions([cond("={{ $json.is_invoice }}", "boolean", "true", single=True)]),
    "options": {},
}, [880, Y])

node("Read Invoice Ledger", "n8n-nodes-base.googleSheets", 4.5, {
    "operation": "read",
    "documentId": {"__rl": True, "mode": "id", "value": "YOUR_GOOGLE_SHEET_ID"},
    "sheetName": {"__rl": True, "mode": "name", "value": "Invoices"},
    "options": {},
}, [1100, Y], executeOnce=True, alwaysOutputData=True)

node("Validate & Check History", "n8n-nodes-base.code", 2, {
    "jsCode": VALIDATE_JS,
}, [1320, Y])

node("Needs Review?", "n8n-nodes-base.if", 2.2, {
    "conditions": conditions([cond("={{ $json.needs_review }}", "boolean", "true", single=True)]),
    "options": {},
}, [1540, Y])

node("Telegram: Ask Approval", "n8n-nodes-base.telegram", 1.2, {
    "operation": "sendAndWait",
    "chatId": "YOUR_TELEGRAM_CHAT_ID",
    "message": "={{ $json.telegram_message }}",
    "approvalOptions": {"values": {
        "approvalType": "double",
        "approveLabel": "✅ Approve",
        "disapproveLabel": "❌ Reject",
    }},
    "options": {},
}, [1780, Y - 120], webhookId=nid())

node("Apply Approval Decision", "n8n-nodes-base.code", 2, {
    "mode": "runOnceForEachItem",
    "jsCode": APPLY_DECISION_JS,
}, [2000, Y - 120])

node("Mark Auto-Approved", "n8n-nodes-base.code", 2, {
    "mode": "runOnceForEachItem",
    "jsCode": AUTO_APPROVE_JS,
}, [1780, Y + 120])

node("Shape Ledger Row", "n8n-nodes-base.code", 2, {
    "mode": "runOnceForEachItem",
    "jsCode": SHAPE_ROW_JS,
}, [2220, Y])

node("Append to Invoice Ledger", "n8n-nodes-base.googleSheets", 4.5, {
    "operation": "append",
    "documentId": {"__rl": True, "mode": "id", "value": "YOUR_GOOGLE_SHEET_ID"},
    "sheetName": {"__rl": True, "mode": "name", "value": "Invoices"},
    "columns": {"mappingMode": "autoMapInputData", "value": {}, "matchingColumns": [], "schema": []},
    "options": {},
}, [2440, Y])

node("Approved?", "n8n-nodes-base.if", 2.2, {
    "conditions": conditions([
        cond("={{ $json.status }}", "string", "equals", "approved"),
        cond("={{ $json.status }}", "string", "equals", "auto_approved"),
    ], combinator="or"),
    "options": {},
}, [2660, Y])

node("Post to ERP (configure)", "n8n-nodes-base.httpRequest", 4.2, {
    "method": "POST",
    "url": "https://YOUR-ERP.example.com/api/purchase-invoices",
    "sendBody": True,
    "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ ...$json, line_items: JSON.parse($json.line_items_json || '[]') }) }}",
    "options": {},
}, [2880, Y - 100], disabled=True)

# ---------------------------------------------------------------- sticky notes

def sticky(content, pos, w, h, color=None):
    p = {"content": content, "height": h, "width": w}
    if color:
        p["color"] = color
    nodes.append({"parameters": p, "id": nid(), "name": f"Note {len([n for n in nodes if n['type'].endswith('stickyNote')]) + 1}",
                  "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": pos})

sticky("""## 🧾 AI Invoice Processing (Accounts Payable Automation)
Supplier invoices arrive by e-mail → AI reads the PDF → math, duplicate and price checks against your history → clean invoices are auto-approved, risky ones go to Telegram for one-tap approval → everything is logged to a Google Sheet ledger → approved invoices can be pushed to your ERP (SAP, Odoo, QuickBooks, Logo, …).

**Catches:** duplicate invoices · arithmetic errors · unit-price increases vs the last invoice · unusual totals · new vendors · low-confidence reads.""", [-40, Y - 420], 760, 240, 4)

sticky("""### ⚙️ Setup (10 min)
1. **Gmail Trigger** – connect the inbox that receives invoices (adjust the search filter if needed).
2. **AI: Extract Invoice Data** – add your OpenAI credential (model: gpt-4o-mini, change if you like).
3. **Google Sheet** – create a sheet with a tab named `Invoices` and the header row from the README; paste its ID into both Sheets nodes.
4. **Telegram** – add your bot credential and your chat ID.
5. **Validate & Check History** – set your approval policy in `CONFIG` (auto-approve limit, price-increase %, …).
6. *(Optional)* enable **Post to ERP** and point it to your ERP's API.""", [780, Y - 420], 720, 280, 6)

sticky("""### 👤 Human in the loop
Only invoices with at least one warning reach the approver. The Telegram message lists every reason (e.g. *"Price up 18%: Steel sheet 2mm 4.20 → 4.96"*), so the decision takes seconds.""", [1700, Y - 400], 520, 180, 5)

sticky("""### 🔌 ERP hand-off (disabled by default)
Replace the URL/auth with your ERP endpoint (SAP S/4HANA OData, Odoo, QuickBooks, Xero, Logo, Netsis…). Only approved / auto-approved invoices reach this node.""", [2780, Y + 170], 400, 200, 3)

# ---------------------------------------------------------------- connections

def link(a, b, out_index=0):
    return (a, b, out_index)

links = [
    ("Gmail Trigger", "Extract PDF Text", 0),
    ("Extract PDF Text", "AI: Extract Invoice Data", 0),
    ("AI: Extract Invoice Data", "Parse AI Output", 0),
    ("Parse AI Output", "Only Invoices", 0),
    ("Only Invoices", "Read Invoice Ledger", 0),
    ("Read Invoice Ledger", "Validate & Check History", 0),
    ("Validate & Check History", "Needs Review?", 0),
    ("Needs Review?", "Telegram: Ask Approval", 0),   # true
    ("Needs Review?", "Mark Auto-Approved", 1),       # false
    ("Telegram: Ask Approval", "Apply Approval Decision", 0),
    ("Apply Approval Decision", "Shape Ledger Row", 0),
    ("Mark Auto-Approved", "Shape Ledger Row", 0),
    ("Shape Ledger Row", "Append to Invoice Ledger", 0),
    ("Append to Invoice Ledger", "Approved?", 0),
    ("Approved?", "Post to ERP (configure)", 0),
]

connections = {}
for a, b, i in links:
    outs = connections.setdefault(a, {"main": []})["main"]
    while len(outs) <= i:
        outs.append([])
    outs[i].append({"node": b, "type": "main", "index": 0})

workflow = {
    "name": "AI Invoice Processing – Gmail → AI → Approval → Ledger/ERP",
    "nodes": nodes,
    "connections": connections,
    "active": False,
    "settings": {"executionOrder": "v1"},
    "pinData": {},
    "meta": {"templateCredsSetupCompleted": False},
    "tags": [],
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(workflow, f, ensure_ascii=False, indent=2)
print("wrote", OUT, "nodes:", len(nodes))
