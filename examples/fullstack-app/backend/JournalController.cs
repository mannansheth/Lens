using Microsoft.AspNetCore.Mvc;
using System.Net.Http;
namespace Fullstack;
[ApiController]
[Route("api/journal")]
public class JournalController : Controller {
    private readonly JournalRepository repository;
    private readonly HttpClient client;
    public JournalController(JournalRepository repository, HttpClient client) {
        this.repository = repository;
        this.client = client;
    }
    [HttpPost]
    public async Task<IActionResult> Create(JournalEntry entry) {
        var analysis = await client.PostAsync("http://localhost:8000/analyze", null);
        repository.Save(entry.Content);
        return Ok(analysis);
    }
}
public class JournalEntry {
    public string Content { get; set; }
}
public class JournalRepository {
    public void Save(string content) {
        connection.Execute("INSERT INTO journal_entries (content) VALUES (@content)", new { content });
    }
}
