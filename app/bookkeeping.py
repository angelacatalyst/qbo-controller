{% extends "base.html" %}
{% block title %}Categorization — {{ company.company_name }}{% endblock %}

{% block extra_css %}
<style>
  .conf-high   { color: #16a34a; font-weight: 600; }
  .conf-medium { color: #d97706; font-weight: 600; }
  .conf-low    { color: #dc2626; font-weight: 600; }
  .status-pending  { background:#dbeafe; color:#1e40af; }
  .status-approved { background:#dcfce7; color:#166534; }
  .status-applied  { background:#f0fdf4; color:#15803d; }
  .status-rejected { background:#fee2e2; color:#991b1b; }
  .status-badge { padding:2px 8px; border-radius:20px; font-size:11px; font-weight:600; }
  .action-btn { font-size:12px; padding:3px 10px; }
  .proposal-row td { vertical-align:middle; font-size:13px; }
  .acct-arrow { color:#94a3b8; }
</style>
{% endblock %}

{% block content %}
<div class="topbar">
  <div class="company-context">
    <strong>{{ company.company_name }}</strong>
    <span class="realm-badge">{{ company.realm_id }}</span>
    <span class="text-muted" style="font-size:12px;">/ Categorization</span>
  </div>
</div>

<div class="container-fluid p-4">

  {% if saved %}
  <div class="alert alert-success alert-dismissible d-flex align-items-center gap-2 mb-3" role="alert">
    <i class="bi bi-check-circle-fill"></i> {{ saved }}
    <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
  </div>
  {% endif %}

  <!-- Run Engine Card -->
  <div class="row g-3 mb-4">
    <div class="col-md-4">
      <div class="kpi-card h-100">
        <div class="kpi-label">PENDING REVIEW</div>
        <div class="kpi-value text-warning">{{ pending | length }}</div>
      </div>
    </div>
    <div class="col-md-4">
      <div class="kpi-card h-100">
        <div class="kpi-label">APPROVED (READY TO APPLY)</div>
        <div class="kpi-value text-success">{{ approved | length }}</div>
      </div>
    </div>
    <div class="col-md-4">
      <div class="kpi-card h-100">
        <div class="kpi-label">APPLIED TO QBO</div>
        <div class="kpi-value text-primary">{{ applied | length }}</div>
      </div>
    </div>
  </div>

  <div class="d-flex gap-3 align-items-center mb-4">
    <form method="post" action="/company/{{ company.realm_id }}/categorize" class="d-flex align-items-center gap-2">
      <label class="text-muted" style="font-size:13px;">Look back</label>
      <select name="lookback_days" class="form-select form-select-sm" style="width:100px;">
        <option value="30">30 days</option>
        <option value="60">60 days</option>
        <option value="90" selected>90 days</option>
        <option value="180">180 days</option>
      </select>
      <button class="btn btn-primary btn-sm">
        <i class="bi bi-search"></i> Run Categorization Engine
      </button>
    </form>
    <span class="text-muted" style="font-size:12px;">
      Scans uncategorized / Ask My Accountant transactions and proposes accounts.
      All proposals require your approval before any QBO write.
    </span>
  </div>

  <!-- Pending Proposals -->
  {% if pending %}
  <div class="card mb-4" style="border:1px solid #e2e8f0; border-radius:12px;">
    <div class="card-header bg-white py-3 d-flex align-items-center justify-content-between">
      <span class="fw-bold"><i class="bi bi-clock text-warning me-2"></i>Pending Review ({{ pending | length }})</span>
      <button class="btn btn-outline-success btn-sm" onclick="approveAll()">
        <i class="bi bi-check-all"></i> Approve All High Confidence
      </button>
    </div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table class="table table-hover mb-0">
          <thead class="table-light">
            <tr>
              <th style="width:90px;">Date</th>
              <th>Payee / Memo</th>
              <th style="width:100px;">Amount</th>
              <th>Current Account</th>
              <th></th>
              <th>Suggested Account</th>
              <th style="width:70px;">Conf.</th>
              <th>Reason</th>
              <th style="width:150px;">Actions</th>
            </tr>
          </thead>
          <tbody>
            {% for c in pending %}
            <tr class="proposal-row" id="row-{{ c.id }}">
              <td>{{ c.txn_date | fmt_date }}</td>
              <td>
                <div class="fw-medium">{{ c.payee_name or "—" }}</div>
                {% if c.memo %}<div class="text-muted" style="font-size:11px;">{{ c.memo[:60] }}</div>{% endif %}
              </td>
              <td class="fw-bold">{{ c.amount | currency }}</td>
              <td class="text-muted">{{ c.current_account_name or "—" }}</td>
              <td class="acct-arrow">→</td>
              <td class="fw-medium text-success">{{ c.suggested_account_name or "—" }}</td>
              <td>
                <span class="conf-{{ c.confidence }}">{{ c.confidence | upper }}</span>
              </td>
              <td class="text-muted" style="font-size:12px;">{{ c.reason[:80] if c.reason else "—" }}</td>
              <td>
                <div class="d-flex gap-1">
                  <button class="btn btn-success action-btn" onclick="approveOne({{ c.id }})">
                    <i class="bi bi-check"></i>
                  </button>
                  <button class="btn btn-outline-danger action-btn" onclick="rejectOne({{ c.id }})">
                    <i class="bi bi-x"></i>
                  </button>
                </div>
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
  {% else %}
  <div class="alert alert-light border mb-4">
    <i class="bi bi-inbox me-2 text-muted"></i>No pending proposals.
    Run the categorization engine to scan for uncategorized transactions.
  </div>
  {% endif %}

  <!-- Approved — ready to apply -->
  {% if approved %}
  <div class="card mb-4" style="border:1px solid #bbf7d0; border-radius:12px;">
    <div class="card-header bg-white py-3 d-flex align-items-center justify-content-between">
      <span class="fw-bold"><i class="bi bi-check-circle text-success me-2"></i>Approved — Ready to Apply ({{ approved | length }})</span>
      <span class="text-muted" style="font-size:12px;">Click Apply to write each change to QBO</span>
    </div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table class="table table-hover mb-0">
          <thead class="table-light">
            <tr>
              <th>Date</th>
              <th>Payee</th>
              <th>Amount</th>
              <th>Current → Suggested</th>
              <th>Confidence</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {% for c in approved %}
            <tr id="row-{{ c.id }}">
              <td>{{ c.txn_date | fmt_date }}</td>
              <td>{{ c.payee_name or "—" }}</td>
              <td class="fw-bold">{{ c.amount | currency }}</td>
              <td>
                <span class="text-muted">{{ c.current_account_name or "—" }}</span>
                <i class="bi bi-arrow-right mx-1 text-muted"></i>
                <span class="text-success fw-medium">{{ c.suggested_account_name }}</span>
              </td>
              <td><span class="conf-{{ c.confidence }}">{{ c.confidence | upper }}</span></td>
              <td>
                <button class="btn btn-success action-btn" onclick="applyOne({{ c.id }})">
                  <i class="bi bi-cloud-upload me-1"></i>Apply to QBO
                </button>
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
  {% endif %}

  <!-- Applied / Rejected history (collapsed) -->
  {% if applied or rejected %}
  <div class="mb-3">
    <button class="btn btn-link btn-sm text-muted p-0" data-bs-toggle="collapse" data-bs-target="#historySection">
      <i class="bi bi-chevron-down me-1"></i>Show history ({{ applied | length }} applied, {{ rejected | length }} rejected)
    </button>
    <div class="collapse mt-2" id="historySection">
      <div class="table-responsive">
        <table class="table table-sm table-bordered mb-0" style="font-size:12px;">
          <thead class="table-light">
            <tr><th>Date</th><th>Payee</th><th>Amount</th><th>Suggested Account</th><th>Status</th></tr>
          </thead>
          <tbody>
            {% for c in (applied + rejected) | sort(attribute='txn_date', reverse=True) %}
            <tr>
              <td>{{ c.txn_date | fmt_date }}</td>
              <td>{{ c.payee_name or "—" }}</td>
              <td>{{ c.amount | currency }}</td>
              <td>{{ c.suggested_account_name }}</td>
              <td><span class="status-badge status-{{ c.status }}">{{ c.status | upper }}</span></td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
  {% endif %}

</div>

<script>
const realmId = "{{ company.realm_id }}";

async function approveOne(catId) {
  const r = await fetch(`/company/${realmId}/categorization/${catId}/approve`, {method:'POST'});
  if (r.ok) {
    const row = document.getElementById(`row-${catId}`);
    if (row) row.style.opacity = '0.4';
    location.reload();
  } else {
    alert('Error approving proposal');
  }
}

async function rejectOne(catId) {
  const reason = prompt('Rejection reason (optional):') || '';
  const fd = new FormData();
  fd.append('reason', reason);
  const r = await fetch(`/company/${realmId}/categorization/${catId}/reject`, {method:'POST', body:fd});
  if (r.ok) location.reload();
  else alert('Error rejecting proposal');
}

async function applyOne(catId) {
  if (!confirm('Apply this categorization to QBO now?')) return;
  const r = await fetch(`/company/${realmId}/categorization/${catId}/apply`, {method:'POST'});
  if (r.ok) location.reload();
  else {
    const data = await r.json().catch(() => ({}));
    alert('Error applying: ' + (data.detail || r.status));
  }
}

async function approveAll() {
  const rows = document.querySelectorAll('[id^="row-"]');
  let count = 0;
  for (const row of rows) {
    const id = row.id.replace('row-', '');
    const r = await fetch(`/company/${realmId}/categorization/${id}/approve`, {method:'POST'});
    if (r.ok) count++;
  }
  if (count > 0) location.reload();
}
</script>
{% endblock %}
