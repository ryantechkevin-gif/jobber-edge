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

INVOICES_QUERY = """
query InvoicesPage($first: Int!, $after: String) {
  invoices(first: $first, after: $after) {
    nodes {
      id
      invoiceNumber
      invoiceStatus
      createdAt
      amounts { total invoiceBalance }
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
# tags, notes (ClientNote.message is the actual note text), properties,
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
