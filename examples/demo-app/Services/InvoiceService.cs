namespace Demo.Services;

using Demo.Models;
using Demo.Repositories;

public class InvoiceService
{
    private readonly InvoiceRepository _repository;

    public InvoiceService(InvoiceRepository repository) { _repository = repository; }

    public IEnumerable<Invoice> GetInvoices(int customerId)
    {
        return _repository.GetInvoices(customerId);
    }

    // Calculate the pending amount still owed on an invoice after payments.
    public decimal CalculatePendingAmount(Invoice invoice)
    {
        return Math.Max(0m, invoice.TotalAmount - invoice.PaidAmount);
    }
}
