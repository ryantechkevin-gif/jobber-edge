"""
GraphQL query strings.

Everything below (except the `emails { address }` / `phones { number }`
shape in CLIENTS_QUERY, still taken on faith from a third-party reference)
is confirmed field-by-field against the live schema via /api/jobber/query
introspection, not guessed. Two things worth knowing from that exploration:

  - Jobber's public API has NO query or mutation anywhere for reading or
    sending actual client communication content (texts/emails). A
    `Client.messages` connection exists, but its edge type has no `node`
    field at all (confirmed even with includeDeprecated) -- it can report
    a bare totalCount and nothing else. Any messaging feature needs its
    own channel entirely outside Jobber.
  - `TaskFilterAttributes` has no clientId filter, so "all tasks for this
    client" can't be fetched directly at the root -- tasks are only
    efficiently enumerable via whatever Job/Quote/Request they're
    attached to (both have their own nested `tasks` connection), not
    globally by client.
"""

ACCOUNT_QUERY = """
query {
  account {
    id
    name
  }
}
"""

CLIENTS_QUERY = """
query ClientsPage($first: Int!, $after: String) {
  clients(first: $first, after: $after) {
    nodes {
      id
      name
      isCompany
      isArchived
      createdAt
      emails { address }
      phones { number }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# `paymentRecords` is a direct nested connection on Invoice (confirmed via
# full-type introspection) -- simpler than trying to trace back from the
# root-level `paymentRecords`/PaymentRecord.allocations union just to find
# which invoice/client a payment belongs to. `entryDate` is the date the
# payment was recorded (the "MARKED PAID" column from the old CSV).
INVOICES_QUERY = """
query InvoicesPage($first: Int!, $after: String) {
  invoices(first: $first, after: $after) {
    nodes {
      id
      invoiceNumber
      invoiceStatus
      createdAt
      issuedDate
      dueDate
      subject
      jobberWebUri
      client { id name companyName }
      jobs(first: 5) {
        nodes { id jobType jobStatus title }
      }
      amounts { total invoiceBalance }
      paymentRecords(first: 10) {
        nodes { id amount entryDate }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Every client with jobs AND properties/custom fields attached in one
# paginated pass -- the single query the ask.py toolbelt fetches to
# answer any question about recurring billing or about a named
# service/ISP/keyword (e.g. "Quantum Fiber"). Filtering happens in
# Python rather than via Jobber's own clients(searchTerm, searchFields)
# args: a live check found that CUSTOM_FIELDS/PROPERTIES search scope
# does NOT reliably reach property-level custom field values (a known
# real value there came back with zero matches), so property custom
# fields -- confirmed via CLIENT_DASHBOARD_QUERY as the real source of
# ISP/network info -- are fetched directly and matched ourselves instead
# of trusted to Jobber's search.
CLIENTS_FULL_QUERY = """
query ClientsFull($first: Int!, $after: String) {
  clients(first: $first, after: $after) {
    nodes {
      id
      name
      companyName
      isArchived
      clientProperties(first: 10) {
        nodes {
          id
          name
          address { street city province postalCode }
          customFields {
            __typename
            ... on CustomFieldText { label valueText }
            ... on CustomFieldDropdown { label valueDropdown }
          }
        }
      }
      jobs(first: 20) {
        nodes {
          id
          jobNumber
          title
          jobType
          jobStatus
          total
          invoicedTotal
          uninvoicedTotal
          startAt
          endAt
          completedAt
          jobberWebUri
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Root-level, generic over any JobFilterAttributes (jobType: RECURRING for
# the recurring book, jobType: ONE_OFF for Incomplete Jobs, or filter: null
# for everything) with client attribution and the autopay flag attached --
# confirmed live (before/after compared against the CLIENTS_FULL_QUERY
# per-client walk: same job records, same jobStatus/total values either
# way) to replace walking every client just to find their jobs.
# `jobStatus` is a UI bucket (active, late, today, upcoming,
# action_required, on_hold, unscheduled, expiring_within_30_days,
# requires_invoicing, archived) confirmed via enum introspection --
# 'archived' is the only one that means the job is actually closed;
# everything else is still ongoing (verified live: one page of recurring
# jobs was 85% "action_required" and only 14% "active", so treating
# jobStatus=='active' as the active filter would massively undercount --
# filter on jobStatus != 'archived' instead). `completedAt` is null for
# an open one-off job regardless of jobStatus bucket, which is what
# Incomplete Jobs actually needs. `willClientBeAutomaticallyCharged` is
# the confirmed source for the "Automatic Payments Enabled/Disabled"
# column from the old CSV export.
JOBS_QUERY = """
query JobsByFilter($first: Int!, $after: String, $filter: JobFilterAttributes) {
  jobs(first: $first, after: $after, filter: $filter) {
    nodes {
      id
      jobNumber
      title
      jobType
      jobStatus
      total
      invoicedTotal
      uninvoicedTotal
      startAt
      endAt
      completedAt
      jobberWebUri
      willClientBeAutomaticallyCharged
      client { id name companyName isArchived }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Root-level visits -- confirmed live: Visit has direct `client`, `job`,
# `property` (address), and `assignedUsers` links, so this is a single
# flat pass with everything the "Visits This Week" section needs, no
# per-job/per-client walking required. Paginated in full and filtered by
# startAt in Python (same pattern as INVOICES_QUERY) rather than using
# the root `filter` arg's date-range shape, which hasn't been confirmed
# yet -- revisit if visit volume ever makes full pagination too slow.
VISITS_QUERY = """
query VisitsPage($first: Int!, $after: String) {
  visits(first: $first, after: $after) {
    nodes {
      id
      title
      startAt
      endAt
      isComplete
      visitStatus
      client { id name companyName }
      job { id jobNumber title }
      property {
        id
        address { street city province postalCode }
      }
      assignedUsers(first: 5) {
        nodes { id name { full } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

QUOTES_QUERY = """
query QuotesPage($first: Int!, $after: String) {
  quotes(first: $first, after: $after) {
    nodes {
      id
      quoteNumber
      title
      quoteStatus
      createdAt
      amounts { total }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Everything about one client in a single call: identity/contact info,
# tags, notes (ClientNote.message is the actual note text -- legacy/rarely
# used at WeSpeakWiFi now; per-property custom fields below are the real
# current source for network info), properties (with their custom fields
# -- e.g. "Network Name", "Equipment", "Eero Network I.D.", "ISP Account
# Number" -- confirmed live against a real property, values are a union
# of concrete types keyed off __typename; only CustomFieldText and
# CustomFieldDropdown are handled below since those are the only two
# actually in use on the account checked so far -- add more `... on
# CustomFieldX` fragments here if another type shows up in the field list),
# jobs, quotes, invoices, and requests. Deliberately excludes `messages`
# (see module docstring -- it's real but content-less) and doesn't attempt
# a client-wide task rollup (no clientId filter exists on tasks; use
# Job/Quote/Request's own nested `tasks` connection instead if needed).
# `first: 10` per connection is a reasonable dashboard-sized snapshot --
# raise it or add pagination if a client legitimately has more history
# than that worth seeing.
CLIENT_DASHBOARD_QUERY = """
query ClientDashboard($id: EncodedId!) {
  client(id: $id) {
    id
    name
    firstName
    lastName
    companyName
    isCompany
    isLead
    isArchived
    balance
    createdAt
    updatedAt
    email
    phone
    jobberWebUri
    tags(first: 20) {
      nodes { id label }
    }
    notes(first: 10) {
      nodes { id message pinned createdAt }
    }
    clientProperties(first: 10) {
      nodes {
        id
        name
        jobberWebUri
        address { street city province postalCode }
        customFields {
          __typename
          ... on CustomFieldText { label valueText }
          ... on CustomFieldDropdown { label valueDropdown }
        }
      }
    }
    jobs(first: 10) {
      nodes {
        id
        jobNumber
        title
        jobStatus
        jobType
        total
        invoicedTotal
        uninvoicedTotal
        startAt
        endAt
        completedAt
        jobberWebUri
      }
    }
    quotes(first: 10) {
      nodes {
        id
        quoteNumber
        title
        quoteStatus
        message
        sentAt
        jobberWebUri
        amounts { total }
      }
    }
    invoices(first: 10) {
      nodes {
        id
        invoiceNumber
        invoiceStatus
        subject
        message
        issuedDate
        dueDate
        jobberWebUri
        amounts { total invoiceBalance }
      }
    }
    requests(first: 10) {
      nodes {
        id
        requestStatus
        title
        createdAt
        jobberWebUri
      }
    }
  }
}
"""

# Ad-hoc schema exploration -- see the /api/jobber/schema route. Includes
# field arguments (needed to actually call anything -- e.g. pagination
# params, filters) and unwraps NON_NULL/LIST wrapper types three levels
# deep, which covers shapes like `[ClientEdge!]!`.
INTROSPECT_TYPE_QUERY = """
query IntrospectType($name: String!) {
  __type(name: $name) {
    name
    kind
    fields {
      name
      args {
        name
        type { name kind ofType { name kind ofType { name kind } } }
      }
      type { name kind ofType { name kind ofType { name kind } } }
    }
  }
}
"""
