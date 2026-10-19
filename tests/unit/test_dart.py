from lens.repository.indexing import parse_repository


def write(root, path, source):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def test_dart_flutter_symbols_imports_and_literal_http_requests(tmp_path):
    write(tmp_path, "flutter/lib/core/api_client.dart", "class ApiClient {}\n")
    write(tmp_path, "flutter/lib/services/profile_service.dart", """
import '../core/api_client.dart';

class ProfileService {
  const ProfileService(this.client);
  final ApiClient client;

  Future<void> updateProfile(String userId) async {
    await client.postJson('/api/users/$userId', {});
  }
}
""")
    write(tmp_path, "flutter/lib/screens/profile_screen.dart", """
import 'package:flutter/material.dart';
import '../services/profile_service.dart';

class ProfileScreen extends StatelessWidget {
  const ProfileScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return const SizedBox();
  }
}
""")
    write(tmp_path, "backend/routes.js", """
const express = require('express');
const router = express.Router();
router.post('/api/users/:userId', async (req, res) => res.json({ success: true }));
module.exports = router;
""")
    write(tmp_path, "flutter/.dart_tool/generated.dart", "class Generated {}\n")
    knowledge = parse_repository(tmp_path)

    dart_files = {file.path for file in knowledge.files if file.language == "dart"}
    assert "flutter/lib/screens/profile_screen.dart" in dart_files
    assert "flutter/.dart_tool/generated.dart" not in dart_files

    symbols = {symbol.qualified_name: symbol for symbol in knowledge.symbols}
    screen = symbols["flutter/lib/screens/profile_screen.dart::ProfileScreen"]
    service_method = symbols["flutter/lib/services/profile_service.dart::ProfileService.updateProfile"]
    assert screen.kind == "component"
    assert "client.postJson" in service_method.source
    assert service_method.metadata["parameters"] == ["String userId"]
    assert service_method.metadata["requests"][0]["method"] == "POST"
    assert service_method.metadata["requests"][0]["path"] == "/api/users/:userId"

    relationships = {(edge.kind, edge.target_name, edge.target_id) for edge in knowledge.relationships}
    api_module = symbols["flutter/lib/core/api_client.dart::module"]
    assert any(kind == "IMPORTS_MODULE" and target == "../core/api_client.dart" and target_id == api_module.id
               for kind, target, target_id in relationships)
    assert any(kind == "REQUESTS" and target == "POST /api/users/:userId"
               and target_id == next(symbol.id for symbol in knowledge.symbols
                                     if symbol.kind == "endpoint" and
                                     symbol.name == "POST /api/users/:userId")
               for kind, target, target_id in relationships)
