{{#has_psyches}}
<capability-instructions>
<psyches>
<instruction>Apply these selected psyche prompts as agent behavior guidance.</instruction>
<available>
{{#psyches}}
<psyche name="{{name}}">
{{content}}
</psyche>
{{/psyches}}
</available>
</psyches>
</capability-instructions>
{{/has_psyches}}
