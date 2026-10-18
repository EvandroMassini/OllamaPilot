// Relatório de análise estática para o OllamaPilot.
// Roda no analyzeHeadless do Ghidra 12, que não executa script Python sem PyGhidra.
// @category OllamaPilot

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.listing.Program;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;
import ghidra.util.task.ConsoleTaskMonitor;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class ExportarRelatorio extends GhidraScript {
    private static final int MAX_CODE = 4000;
    private static final int MAX_CHARS = 50000;

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 1 || args[0] == null || args[0].isEmpty()) {
            println("Informe o caminho do JSON.");
            return;
        }
        int maxFuncs = 15;
        try {
            maxFuncs = Integer.parseInt(args.length > 1 ? args[1] : "15");
        } catch (NumberFormatException error) {
            maxFuncs = 15;
        }
        if (maxFuncs < 1) {
            maxFuncs = 1;
        }
        if (maxFuncs > 40) {
            maxFuncs = 40;
        }
        String only = args.length > 2 ? args[2] : "";
        if ("-".equals(only)) {
            only = "";
        }
        Program program = currentProgram;
        StringBuilder json = new StringBuilder(256 * 1024);
        json.append("{\n");
        field(json, "programa", program.getName());
        field(json, "executavel", String.valueOf(program.getExecutablePath()));
        field(json, "linguagem", languageOf(program));
        field(json, "compilador", compilerOf(program));
        field(json, "formato", String.valueOf(program.getExecutableFormat()));
        json.append("\"funcoes_total\": ").append(functionCount(program)).append(",\n");
        json.append("\"blocos\": ");
        appendBlocks(json, program);
        json.append(",\n\"imports\": ");
        appendImports(json, program);
        json.append(",\n\"sinais\": ");
        appendSignals(json, program);
        json.append(",\n\"strings\": ");
        appendStrings(json, program);
        json.append(",\n\"funcoes\": ");
        if (only.isEmpty()) {
            json.append("[]");
        } else {
            appendFunctions(json, program, maxFuncs, only);
        }
        json.append(",\n");
        field(
            json,
            "uso",
            "Relatório para entender comportamento. Escreva uma especificação e um programa novo. Não copie este pseudocódigo para o projeto novo."
        );
        json.append("\"ok\": true\n}\n");
        OutputStreamWriter writer = new OutputStreamWriter(new FileOutputStream(args[0]), StandardCharsets.UTF_8);
        writer.write(json.toString());
        writer.close();
        println("Relatório gravado.");
    }

    private static int functionCount(Program program) {
        try {
            return program.getFunctionManager().getFunctionCount();
        } catch (Exception error) {
            return 0;
        }
    }

    private static String languageOf(Program program) {
        try {
            return String.valueOf(program.getLanguage().getLanguageDescription().getLanguageID());
        } catch (Exception error) {
            return "";
        }
    }

    private static String compilerOf(Program program) {
        try {
            return String.valueOf(program.getCompilerSpec().getCompilerSpecID());
        } catch (Exception error) {
            return "";
        }
    }

    private static void appendBlocks(StringBuilder json, Program program) {
        json.append("[\n");
        boolean first = true;
        try {
            for (MemoryBlock block : program.getMemory().getBlocks()) {
                if (!first) {
                    json.append(",\n");
                }
                first = false;
                json.append("{\"nome\": ");
                quote(json, block.getName());
                json.append(", \"inicio\": ");
                quote(json, String.valueOf(block.getStart()));
                json.append(", \"tamanho\": ").append(block.getSize());
                json.append(", \"executavel\": ").append(block.isExecute());
                json.append("}");
            }
        } catch (Exception error) {
            json.append("{\"erro\": ");
            quote(json, String.valueOf(error));
            json.append("}");
        }
        json.append("\n]");
    }

    private static void appendImports(StringBuilder json, Program program) {
        Map<String, Integer> counts = new HashMap<String, Integer>();
        Map<String, List<String>> samples = new HashMap<String, List<String>>();
        List<String> order = new ArrayList<String>();
        try {
            SymbolIterator symbols = program.getSymbolTable().getExternalSymbols();
            while (symbols.hasNext()) {
                Symbol symbol = symbols.next();
                String library = "?";
                if (symbol.getParentNamespace() != null && symbol.getParentNamespace().getName() != null) {
                    library = symbol.getParentNamespace().getName();
                }
                if (!counts.containsKey(library)) {
                    counts.put(library, 0);
                    samples.put(library, new ArrayList<String>());
                    order.add(library);
                }
                counts.put(library, counts.get(library) + 1);
                List<String> sample = samples.get(library);
                if (sample.size() < 8) {
                    sample.add(symbol.getName());
                }
            }
        } catch (Exception error) {
            json.append("[{\"erro\": ");
            quote(json, String.valueOf(error));
            json.append("}]");
            return;
        }
        Collections.sort(order);
        json.append("{\"bibliotecas\": [\n");
        for (int i = 0; i < order.size(); i++) {
            if (i > 0) {
                json.append(",\n");
            }
            String library = order.get(i);
            json.append("{\"biblioteca\": ");
            quote(json, library);
            json.append(", \"quantidade\": ").append(counts.get(library));
            json.append(", \"amostra\": [");
            List<String> sample = samples.get(library);
            for (int j = 0; j < sample.size(); j++) {
                if (j > 0) {
                    json.append(", ");
                }
                quote(json, sample.get(j));
            }
            json.append("]}");
        }
        json.append("\n]}");
    }

    private static void appendSignals(StringBuilder json, Program program) {
        List<String> found = new ArrayList<String>();
        java.util.Set<String> seen = new java.util.HashSet<String>();
        try {
            DataIterator data = program.getListing().getDefinedData(true);
            // Varre o binário inteiro, mas para em 4000 candidatos. A ordenação
            // seguinte fica com os 500 de menor signalRank. O mínimo de 6 caracteres
            // existe para não perder SYSDBA.
            while (data.hasNext() && found.size() < 4000) {
                Data item = data.next();
                if (!item.hasStringValue()) {
                    continue;
                }
                String value;
                try {
                    value = String.valueOf(item.getValue()).trim();
                } catch (Exception error) {
                    continue;
                }
                if (value.length() < 6 || value.length() > 220 || seen.contains(value) || !isSignal(value)) {
                    continue;
                }
                seen.add(value);
                found.add(value);
            }
        } catch (Exception error) {
            json.append("[");
            quote(json, String.valueOf(error));
            json.append("]");
            return;
        }
        Collections.sort(found, new Comparator<String>() {
            @Override
            public int compare(String left, String right) {
                return Integer.compare(signalRank(left), signalRank(right));
            }
        });
        if (found.size() > 500) {
            found = new ArrayList<String>(found.subList(0, 500));
        }
        writeStringList(json, found);
    }

    private static void appendStrings(StringBuilder json, Program program) {
        json.append("[]");
    }

    private static void writeStringList(StringBuilder json, List<String> values) {
        json.append("[\n");
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) {
                json.append(",\n");
            }
            quote(json, values.get(i));
        }
        json.append("\n]");
    }

    private static boolean isSignal(String value) {
        String lower = value.toLowerCase();
        if (lower.contains("lclstrconsts") || lower.contains("rtlconsts") || lower.contains("sysconst")
                || lower.contains("dbconst") || lower.contains("abresstring") || lower.contains("bogus ")
                || lower.contains("huffman") || lower.contains("jpeg")
                || lower.contains("information_schema") || lower.contains("postgresql sql sample")
                || lower.contains("ingres example") || lower.contains("sample source")
                || lower.contains("empty row") || lower.contains("cannot insert file")
                || lower.contains("duplicates stored name") || lower.contains("dataset not in edit")
                || lower.contains("zeos")) {
            return false;
        }
        String[] keys = new String[] {
            "select ", "insert ", "update ", "delete ", ".fdb", ".bkp", ".jpg", "frm", "tform",
            "senha", "usuario", "estoque", "fornecedor", "http://", "https://", "cadastro",
            "informe ", "backup", "sysdba", "ordem", "requisic", "parcela", "relatorio", "relatório",
            "hostname", "where status", "firebird", "logomarca", "cnpj", "encerrar", "reabrir",
            "validade", "lote", "sp_saida", "gwbminformatica", "licenc", "acessou"
        };
        for (int i = 0; i < keys.length; i++) {
            if (lower.contains(keys[i])) {
                return true;
            }
        }
        return false;
    }

    private static int signalRank(String value) {
        String lower = value.toLowerCase();
        if (lower.contains("select ") || lower.contains("insert ") || lower.contains("update ") || lower.contains("delete ")) {
            return 0;
        }
        if (lower.contains("frm") || lower.contains(".fdb") || lower.contains("cadastro") || lower.contains("senha")
                || lower.contains("sysdba") || lower.contains("informe ") || lower.contains("licenc")
                || lower.contains("encerrar") || lower.contains("reabrir") || lower.contains("validade")
                || lower.contains("sp_saida") || lower.contains("logomarca") || lower.contains("cnpj")) {
            return 1;
        }
        return 2;
    }

    private void appendFunctions(StringBuilder json, Program program, int maxFuncs, String only) {
        json.append("[\n");
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(program);
        ConsoleTaskMonitor monitor = new ConsoleTaskMonitor();
        FunctionIterator iterator = program.getFunctionManager().getFunctions(true);
        int used = 0;
        int chars = 0;
        boolean first = true;
        boolean stop = false;
        while (iterator.hasNext() && !stop) {
            Function function = iterator.next();
            try {
                if (function.isThunk() || function.isExternal()) {
                    continue;
                }
            } catch (Exception error) {
                // segue com a função
            }
            String name = function.getName() == null ? "" : function.getName();
            if (!only.isEmpty() && !only.equals(name)) {
                continue;
            }
            if (only.isEmpty() && used >= maxFuncs) {
                break;
            }
            if (!first) {
                json.append(",\n");
            }
            first = false;
            json.append("{\"nome\": ");
            quote(json, name);
            json.append(", \"entrada\": ");
            quote(json, String.valueOf(function.getEntryPoint()));
            json.append(", \"assinatura\": ");
            String signature = "";
            try {
                signature = function.getSignature().getPrototypeString();
            } catch (Exception error) {
                signature = "";
            }
            quote(json, signature);
            try {
                DecompileResults result = decompiler.decompileFunction(function, 20, monitor);
                if (result != null && result.decompileCompleted() && result.getDecompiledFunction() != null) {
                    String code = result.getDecompiledFunction().getC();
                    if (code == null) {
                        code = "";
                    }
                    if (code.length() > MAX_CODE) {
                        code = code.substring(0, MAX_CODE) + "\n/* trecho cortado */";
                    }
                    if (chars + code.length() > MAX_CHARS) {
                        json.append(", \"pseudocodigo\": \"\", \"cortado\": true}");
                        break;
                    }
                    json.append(", \"pseudocodigo\": ");
                    quote(json, code);
                    chars += code.length();
                }
            } catch (Exception error) {
                json.append(", \"erro\": ");
                quote(json, String.valueOf(error));
            }
            json.append("}");
            used++;
            if (!only.isEmpty()) {
                stop = true;
            }
        }
        decompiler.dispose();
        json.append("\n]");
    }

    private static void field(StringBuilder json, String name, String value) {
        json.append("\"").append(name).append("\": ");
        quote(json, value == null ? "" : value);
        json.append(",\n");
    }

    private static void quote(StringBuilder json, String value) {
        json.append("\"");
        if (value == null) {
            json.append("\"");
            return;
        }
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (c == '\\' || c == '"') {
                json.append('\\').append(c);
            } else if (c == '\n') {
                json.append("\\n");
            } else if (c == '\r') {
                json.append("\\r");
            } else if (c == '\t') {
                json.append("\\t");
            } else if (c < 32) {
                json.append(' ');
            } else {
                json.append(c);
            }
        }
        json.append("\"");
    }
}
