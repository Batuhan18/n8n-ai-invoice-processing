// Runs the workflow's code nodes and the OpenAI body expression with mock n8n globals.
const fs = require('fs');
const assert = require('assert');
const wf = JSON.parse(fs.readFileSync(require('path').join(__dirname, '..', 'ai-invoice-processing.workflow.json'), 'utf8'));
const byName = Object.fromEntries(wf.nodes.map(n => [n.name, n]));
const code = name => byName[name].parameters.jsCode;

function run(name, ctx) {
  const fn = new Function('$', '$json', '$input', code(name));
  return fn(ctx.$, ctx.$json, ctx.$input);
}

// ---------- 1) OpenAI body expression
const expr = byName['AI: Extract Invoice Data'].parameters.jsonBody;
const inner = expr.replace(/^=\{\{/, '').replace(/\}\}$/, '');
const email = { subject: 'Fatura #A-102', id: 'm1', from: { value: [{ address: 'billing@acme.com' }], text: 'Acme <billing@acme.com>' } };
const body = new Function('$', '$json', 'return ' + inner)(
  n => ({ item: { json: email } }), { text: 'INVOICE A-102 ...' });
const parsed = JSON.parse(body);
assert.equal(parsed.response_format.json_schema.strict, true);
assert.ok(parsed.messages[1].content.includes('Fatura #A-102'));
// strict mode: every property must be required
const sch = parsed.response_format.json_schema.schema;
assert.deepEqual(Object.keys(sch.properties).sort(), [...sch.required].sort());
const li = sch.properties.line_items.items;
assert.deepEqual(Object.keys(li.properties).sort(), [...li.required].sort());
console.log('✓ OpenAI body is valid JSON and strict schema is consistent');

// ---------- 2) Parse AI Output
const aiInv = {
  is_invoice: true, vendor_name: 'Acme Steel Ltd', vendor_tax_id: 'TR1234567890', invoice_number: 'A-102',
  invoice_date: '2026-11-05', due_date: '2026-12-05', currency: 'EUR', subtotal: 1000, tax_total: 200, total: 1200,
  line_items: [{ description: 'Steel sheet 2mm', quantity: 100, unit_price: 5, amount: 500 },
               { description: 'Bolts M8', quantity: 1000, unit_price: 0.5, amount: 500 }],
  confidence: 0.95, notes: '' };
const parsedOut = run('Parse AI Output', {
  $: n => ({ item: { json: email } }),
  $json: { choices: [{ message: { content: JSON.stringify(aiInv) } }] },
});
assert.equal(parsedOut.json.email_from, 'billing@acme.com');
assert.equal(parsedOut.json.invoice_number, 'A-102');
const broken = run('Parse AI Output', { $: n => ({ item: { json: email } }), $json: { choices: [{ message: { content: 'not json' } }] } });
assert.equal(broken.json.is_invoice, false);
console.log('✓ Parse AI Output handles good and broken AI responses');

// ---------- 3) Validate & Check History
const ledger = [
  { vendor_name: 'ACME Steel Ltd.', vendor_tax_id: 'TR1234567890', invoice_number: 'A-090', invoice_date: '2026-10-01',
    total: 1100, status: 'approved',
    line_items_json: JSON.stringify([{ description: 'Steel sheet 2mm', unit_price: 4.2 }, { description: 'Bolts M8', unit_price: 0.5 }]) },
  { vendor_name: 'Old Vendor', vendor_tax_id: '', invoice_number: 'X-1', invoice_date: '2026-09-01', total: 50, status: 'auto_approved', line_items_json: '[]' },
];
function validate(invoices, ledgerRows) {
  return run('Validate & Check History', {
    $: n => ({ all: () => ledgerRows.map(j => ({ json: j })) }),
    $input: { all: () => invoices.map(j => ({ json: j })) },
  });
}
const base = { ...parsedOut.json };

// a) known vendor, price increase 4.2 -> 5 (19%) and total over limit (1200 > 1000)
let [r] = validate([base], ledger);
assert.equal(r.json.needs_review, true);
assert.ok(/Price up 19\.0%/.test(r.json.review_reasons), r.json.review_reasons);
assert.ok(/auto-approve limit/.test(r.json.review_reasons));
assert.ok(!/First invoice/.test(r.json.review_reasons));
assert.ok(r.json.telegram_message.startsWith('🧾 Invoice needs approval'));
console.log('✓ price increase + amount limit flagged:\n   ', r.json.review_reasons);

// b) clean small invoice from known vendor -> auto-approve
const clean = { ...base, invoice_number: 'A-103', subtotal: 450, tax_total: 90, total: 540,
  line_items: [{ description: 'Steel sheet 2mm', quantity: 100, unit_price: 4.3, amount: 430 }, { description: 'Bolts M8', quantity: 40, unit_price: 0.5, amount: 20 }] };
[r] = validate([clean], ledger);
assert.equal(r.json.needs_review, false, r.json.review_reasons);
console.log('✓ clean invoice is auto-approved (no reasons)');

// c) duplicate of A-090 + math error
const dup = { ...clean, invoice_number: 'A-090', total: 999 };
[r] = validate([dup], ledger);
assert.ok(/POSSIBLE DUPLICATE/.test(r.json.review_reasons));
assert.ok(/but total is 999/.test(r.json.review_reasons));
console.log('✓ duplicate and arithmetic error flagged');

// d) same invoice twice in one batch + new vendor + empty ledger
const nv = { ...clean, vendor_name: 'Brand New GmbH', vendor_tax_id: null, invoice_number: 'N-1' };
const res = validate([nv, nv], [{}]);
assert.ok(/First invoice from this vendor/.test(res[0].json.review_reasons));
assert.ok(!/DUPLICATE/.test(res[0].json.review_reasons));
assert.ok(/DUPLICATE/.test(res[1].json.review_reasons));
assert.deepEqual(res.map(x => x.pairedItem.item), [0, 1]);
console.log('✓ new vendor, in-batch duplicate and empty ledger handled');

// ---------- 4) Decision / auto-approve / ledger shaping
const validated = validate([clean], ledger)[0].json;
let d = run('Apply Approval Decision', { $: n => ({ item: { json: validated } }), $json: { data: { approved: true } } });
assert.equal(d.json.status, 'approved');
d = run('Apply Approval Decision', { $: n => ({ item: { json: validated } }), $json: { data: { approved: false } } });
assert.equal(d.json.status, 'rejected');
const a = run('Mark Auto-Approved', { $json: validated });
assert.equal(a.json.status, 'auto_approved');
const row = run('Shape Ledger Row', { $json: a.json });
assert.equal(Object.keys(row.json).length, 18);
assert.ok(!('telegram_message' in row.json) && !('line_items' in row.json));
// round-trip: a shaped row must work as ledger history
[r] = validate([{ ...clean, invoice_number: 'A-104' }], [row.json]);
assert.ok(!/First invoice/.test(r.json.review_reasons));
console.log('✓ approval decision, auto-approve and ledger row (round-trip into history) work');
console.log('\nALL TESTS PASSED');
