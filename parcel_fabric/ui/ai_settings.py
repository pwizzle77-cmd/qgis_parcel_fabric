"""AI service settings stored in this QGIS profile, separate from the fabric."""
from qgis.core import QgsSettings
from qgis.gui import QgsAuthConfigSelect
from qgis.PyQt.QtWidgets import (
    QComboBox, QFormLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSizePolicy, QWidget,
)

from ..core import ai_service as AI
from ..core import ollama


class AISettingsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._drafts = {}
        self._current = None
        form = QFormLayout(self)
        self.provider = QComboBox()
        for key, (label, _) in AI.PROVIDERS.items():
            self.provider.addItem(label, key)
        self.url = QLineEdit()
        self.url.setPlaceholderText("API base URL, e.g. https://service.example/v1")
        self.auth = QgsAuthConfigSelect(self)
        self.auth.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.ocr_model, self.llm_model = QComboBox(), QComboBox()
        for combo in (self.ocr_model, self.llm_model):
            combo.setEditable(True)
            combo.setMinimumContentsLength(22)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        intro = QLabel("These settings belong to your QGIS profile, not the shared fabric. "
                       "Page reading needs a vision model; description/call extraction needs a chat model.")
        intro.setWordWrap(True)
        key_tip = QLabel("For an API key, create/select a QGIS Basic authentication configuration: "
                         "set Username to api-key and put the key in Password. QGIS encrypts the credential "
                         "and may ask for its master password. Leave empty for servers without authentication.")
        key_tip.setWordWrap(True)
        find = QPushButton("Find Models / Test Connection")
        find.clicked.connect(self._find_models)
        form.addRow(intro)
        form.addRow("Service", self.provider)
        form.addRow("API base URL", self.url)
        form.addRow("API credentials", self.auth)
        form.addRow(key_tip)
        form.addRow("Page-reading model", self.ocr_model)
        form.addRow("Description / calls model", self.llm_model)
        form.addRow(find)
        form.addRow(self.notice)
        self.provider.setCurrentIndex(self.provider.findData(AI.load(QgsSettings())["provider"]))
        self.provider.currentIndexChanged.connect(self._switch)
        self.url.textChanged.connect(self._notice)
        self._switch()

    def values(self):
        return dict(provider=self.provider.currentData(), url=self.url.text().strip(),
                    ocr_model=self.ocr_model.currentText().strip(),
                    llm_model=self.llm_model.currentText().strip(), authcfg=self.auth.configId())

    def _switch(self):
        if self._current is not None:
            previous = self.values()
            previous["provider"] = self._current
            self._drafts[self._current] = previous
        self._current = self.provider.currentData()
        values = self._drafts.get(self._current) or AI.load(QgsSettings(), self._current)
        self.url.setText(values["url"])
        self.auth.setConfigId(values["authcfg"])
        for combo, key in ((self.ocr_model, "ocr_model"), (self.llm_model, "llm_model")):
            combo.clear()
            combo.setCurrentText(values[key])
        self.auth.setEnabled(self._current != "ollama")
        self._notice()

    def _notice(self):
        if self.provider.currentData() == "ollama":
            text = "Ollama uses only this computer. Choose downloaded local models."
        else:
            text = ("AI requests send deed text or page images to: %s. A hosted service may charge "
                    "for usage. Model discovery sends no deed content. The parser and manual entry "
                    "work without AI." % (self.url.text().strip() or "the configured service"))
        self.notice.setText(text)

    def _find_models(self):
        try:
            models = AI.client(self.values()).list_models()
        except AI.AIError as error:
            QMessageBox.warning(self, "AI Services", str(error))
            return
        for combo, prefs in ((self.ocr_model, ("deepseek-ocr", "qwen2.5vl", "gemma4")),
                             (self.llm_model, ("qwen2.5vl", "gemma4", "qwen2.5-coder"))):
            previous = combo.currentText()
            combo.clear()
            combo.addItems(models)
            combo.setCurrentText(previous or (ollama.pick(models, *prefs)
                                             if self._current == "ollama" else ""))
        QMessageBox.information(self, "AI Services", "Connected. Found %d model(s). "
                                "Choose models with the capabilities you need, or type their IDs." % len(models))

    def save(self):
        values = self.values()
        self._drafts[values["provider"]] = values
        drafts = [v for v in self._drafts.values() if v["url"] or v["provider"] == values["provider"]]
        for item in drafts:
            AI.validate(item)
        settings = QgsSettings()
        for item in drafts:
            AI.save(settings, item)
        settings.setValue(AI.PREFIX + "provider", values["provider"])
