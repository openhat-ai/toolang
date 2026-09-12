<capability-catalog>
{{#has_skills}}
<skills>
<available>
{{#skills}}
<skill name="{{name}}" scope="{{scope}}" origin="{{origin}}" form="{{form}}" ref="{{ref}}">
{{#description}}
<description>{{description}}</description>
{{/description}}
{{#metadata_items}}
<metadata key="{{key}}">{{value}}</metadata>
{{/metadata_items}}
</skill>
{{/skills}}
</available>
</skills>
{{/has_skills}}

{{#has_services}}
<services>
<available>
{{#services}}
<service name="{{name}}" scope="{{scope}}" origin="{{origin}}" form="{{form}}" ref="{{ref}}">
{{#description}}
<description>{{description}}</description>
{{/description}}
{{#metadata_items}}
<metadata key="{{key}}">{{value}}</metadata>
{{/metadata_items}}
</service>
{{/services}}
</available>
</services>
{{/has_services}}
</capability-catalog>
