def helper():
    from app.modules.billing.repository import InvoiceRepository   # function-level: not part of the cycle graph
    return InvoiceRepository
