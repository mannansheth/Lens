CREATE TABLE Customers (
    Id INT PRIMARY KEY,
    Name VARCHAR(200) NOT NULL
);

CREATE TABLE Invoices (
    Id INT PRIMARY KEY,
    CustomerId INT NOT NULL,
    TotalAmount DECIMAL(12,2) NOT NULL,
    PaidAmount DECIMAL(12,2) NOT NULL DEFAULT 0,
    CONSTRAINT fk_invoice_customer FOREIGN KEY (CustomerId) REFERENCES Customers(Id),
    INDEX ix_invoice_customer (CustomerId)
);

CREATE VIEW UnpaidInvoices AS
SELECT Id, CustomerId, TotalAmount - PaidAmount AS PendingAmount
FROM Invoices WHERE TotalAmount > PaidAmount;

DELIMITER $$
CREATE PROCEDURE GetInvoices(IN customerId INT)
BEGIN
    SELECT Id, CustomerId, TotalAmount, PaidAmount
    FROM Invoices WHERE CustomerId = customerId;
END$$
DELIMITER ;
