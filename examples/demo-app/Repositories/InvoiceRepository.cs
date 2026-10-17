namespace Demo.Repositories;

using System.Data;
using Dapper;
using Demo.Models;

public class InvoiceRepository
{
    private readonly IDbConnection _connection;

    public InvoiceRepository(IDbConnection connection) { _connection = connection; }

    // GetInvoices is a MySQL stored procedure that retrieves customer invoice data.
    public IEnumerable<Invoice> GetInvoices(int customerId)
    {
        return _connection.Query<Invoice>("GetInvoices", new { customerId },
            commandType: CommandType.StoredProcedure);
    }
}
