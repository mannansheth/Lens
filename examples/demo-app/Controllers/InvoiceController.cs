namespace Demo.Controllers;

using Demo.Services;
using Demo.Models;

public class InvoiceController
{
    private readonly InvoiceService _service;

    public InvoiceController(InvoiceService service) { _service = service; }

    // Retrieve customer invoices through the service and repository layers.
    public IEnumerable<Invoice> GetInvoices(int customerId)
    {
        return _service.GetInvoices(customerId);
    }
}
